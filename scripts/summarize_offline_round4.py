"""Read fitted artifacts to write supplemental reports; never changes fitted models."""
import argparse
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from gpu_lab.comparison import write_csv
from gpu_lab.crossgpu.catalog import compile_catalog
from gpu_lab.nvidia.model import NvidiaModel
from gpu_lab.nvidia.demos import conservative_ranking
from gpu_lab.resources import GPUState, ResourceRequest
from gpu_lab.scoring import PodRequest
from gpu_lab.run import save_json, sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results",type=Path,default=ROOT/"results/offline-v4")
    parser.add_argument("--pytest-xml",type=Path)
    args = parser.parse_args()
    out = args.results
    read = lambda file:json.loads((out/file).read_text(encoding="utf-8"))
    plan,results = read("frozen-plan.json"),read("metrics.json")
    variants,per = [],[]
    for name,r in results.items():
        per.extend(r["per_model"])
        model = NvidiaModel.load(out/name/"selected-model.joblib")
        names = model.pipeline.named_steps["imputer"].get_feature_names_out(model.metadata["feature_names"])
        importance = sorted([{"feature":str(k),"impurity_importance":float(v)} for k,v in zip(names,model.pipeline.named_steps["rf"].feature_importances_)],
                            key=lambda row:-row["impurity_importance"])
        write_csv(out/name/"feature-importance.csv",importance)
        config = plan["protocols"][name]
        lines = [f"# {name}","",f"协议：{config['kind']}。全部真值为synthetic理论标签，没有真实性能准确率。", "",
                 "型号/轨迹成员清单见 [split-manifests.json](../split-manifests.json)。新的3款测试型号不参与任何协议拟合/验证；标为诊断的型号已在历史中观察过。", "",
                 f"验证集选中特征 `{r['selected_feature_set']}`；选模冻结见 [选择清单](../selection-frozen-before-test.json)。", "",
                 "| 划分 | 样本 | Accuracy | 固定四类Macro-F1 | 实际类别Macro-F1 | 真值类别数 |", "|---|---|---|---|---|---|"]
        for split,m in r["selected_metrics"].items():
            lines.append(f"| {split} | {m['samples']} | {m['accuracy']:.6f} | {m['macro_f1']:.6f} | {m['macro_f1_observed_classes']:.6f} | {len(m['observed_true_classes'])} |")
        lines += ["", "| 型号 | Accuracy | 错误 | 接受状态/总数 | 接受后错误 | 状态 |", "|---|---|---|---|---|---|"]
        for row in r["per_model"]:
            lines.append(f"| {row['device_id']} | {row['accuracy']:.6f} | {row['errors']} | {row['accepted_states']}/{row['samples']} | {row['accepted_guarded_errors']} | {row['evaluation_status']} |")
        lines += ["", "[完整PR/F1与类别数量](metrics.json) · [混淆矩阵](confusion.png) · [错误样本](test-errors.csv) · [逐状态预测及拒判原因](test-predictions.jsonl)", "",
                  "[特征重要性](feature-importance.csv)为树的杂质重要性，受相关特征与取值范围影响，只用于诊断，不能当作因果解释或泛化证明。", "",
                  "固定四类Macro-F1将没有真值且没有预测的类别记0；实际类别Macro-F1仅平均该划分存在的真值类。两者都给出，不对缺失类别伪造样本。模型拒判与原始RF误差分开记录。"]
        (out/name/"report.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    b = results["B_multi_architecture"]
    comparisons = {r["feature_set"]:r for r in read("B_multi_architecture/validation-selection.json")}
    lines = ["# 第四轮特征消融与结果解释", "", "标签合同、硬件库、状态样本和分组固定。每组只在验证集选择叶节点，主模型只按验证集比较。所有结果属于合成理论规则拟合。", "",
             "| 特征组合 | 验证Acc | 验证Macro-F1(实际类别) | 测试Acc | 测试四类Macro-F1 |", "|---|---|---|---|---|"]
    for variant,m in b["test_feature_ablations"].items():
        c = comparisons[variant]
        lines.append(f"| {variant} | {c['validation_accuracy']:.6f} | {c['validation_observed_macro_f1']:.6f} | {m['accuracy']:.6f} | {m['macro_f1']:.6f} |")
    baseline,both,compute,bandwidth,ratio = (b["test_feature_ablations"][k]["accuracy"] for k in ("baseline14","effective_both","effective_compute","effective_bandwidth","effective_both_balance"))
    lines += ["", f"B协议在相同测试样本上，baseline14={baseline:.6f}，有效算力={compute:.6f}，有效带宽={bandwidth:.6f}，两者一起={both:.6f}。两者组合同时在验证集达到更高表现，因而在测试前选中，并非按测试表现反选。", "",
              "- 绝对容量：no_static_capacity只去掉总容量，相比基线变化小，不能据此断言总容量永远无用。", "- 显存比例：relative_memory同时去掉总/剩余显存绝对值，结果下降；合同中8192/4096MiB是绝对下限，仅比例不能替代绝对剩余量。", "- 单独比例：absolute_only移除两个比例，相对基线差异很小；当前单种子证据不足以宣称比例必然有效。", "- 有效FP32：显式提供原始算力×剩余份额的交互，在本合成任务中比单纯原值和份额更容易被RF使用。", "- 有效带宽：单独加入在B验证集未胜过基线，不能仅凭测试提升就认定它独立有效；但与有效FP32组合后，验证与测试均有明显提升。", f"- 算力/带宽比：effective_both_balance={ratio:.6f}，与effective_both={both:.6f}相同，没有显示额外收益，按预定较少特征规则选择后者。", "",
              "高分的原因包含特征与确定性标签理论的结构一致：两项有效能力把乘法边界变成轴对齐阈值，RF能近似重建参考规则。它不是实际吞吐或共享干扰的发现。Ada单架构协议反而由验证集选中absolute_only，说明一个特征组合不保证在所有训练覆盖下最好。", "",
              "## 未知型号覆盖与失败", ""]
    diagnostic = results["D_a10_diagnostic"]
    old_acc = diagnostic["test_feature_ablations"]["baseline14"]["accuracy"]
    new_acc = diagnostic["selected_metrics"]["test"]["accuracy"]
    lines += [f"D协议A10在本轮固定相同训练/验证/测试划分下：baseline14={old_acc:.6f}，验证集选中的{diagnostic['selected_feature_set']}={new_acc:.6f}。这个受控消融比直接与第三轮50.85%比较更合适；它仍是已经观察过的诊断型号，不是新的独立盲测。", ""]
    fresh = [r for r in b["per_model"] if r["device_id"] in plan["fresh_test_models"]]
    total,accepted = sum(r["samples"] for r in fresh),sum(r["accepted_states"] for r in fresh)
    accepted_models = sum(r["accepted_states"] > 0 for r in fresh)
    errors = sum(r["accepted_guarded_errors"] for r in fresh)
    fresh_stats = {"fresh_test_models":len(fresh),"models_with_any_provisional":accepted_models,"model_fraction":accepted_models/len(fresh),
                   "fresh_test_states":total,"accepted_states":accepted,"accepted_state_fraction":accepted/total,"accepted_guarded_errors":errors,
                   "raw_errors_all_fresh_states":sum(r["errors"] for r in fresh),"truth_kind":"synthetic"}
    save_json(out/"fresh-unknown-coverage.json",fresh_stats)
    lines += [f"B协议三个新的独立型号：{accepted_models}/{len(fresh)}款有暂定分类；状态覆盖{accepted}/{total}={accepted/total:.4f}，接受后合成标签错误{errors}。未接受的5080仍计入原始RF测试指标。B总表的H200为历史诊断，不能当作第四个全新独立型号。", "",
              "A_ampere的A6000因FP32略高于该协议训练范围而保守待验证；B包含更多硬件能力后可以通过范围检查。5080容量16GB低于所有非5080训练设备的24GB下限，即使原始RF答对也不扩张校验范围。H200在B中也超出训练能力范围。", "",
              "A_ada接受全部515个测试状态却出现17个错误，表明局部验证证据不是准确率保证；该反例保留在报告中。D的A10仍有原始分类错误，不能把拒判后的零错误包装成分类器完美。C严格架构外推只覆盖3个真实存在的类别，四类Macro-F1上限0.75，不能宣称完整四类架构泛化。", "",
              "架构元数据只用于寻找相关验证群体，不进入RF；没有架构名时允许足够的数值邻域证据。第三轮的全局缺类拒判已移除，但仍保留严格范围与局部预测符号验证。", "",
              "本合同只覆盖dense FP32及声明的访存量。例如L4在此合同下初始可能输出训练优先，这不代表真实L4不适合推理，更不评价Tensor Core低精度推理。不能把参考合同标签写成GPU永久用途标签。", "",
              "[完整逐型号表](per-model.csv) · [全部消融CSV](ablations.csv) · [B特征重要性](B_multi_architecture/feature-importance.csv)"]
    (out/"ablation-report.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    devices,_ = compile_catalog(out/"source-records.json")
    model = NvidiaModel.load(out/"B_multi_architecture/selected-model.joblib")
    ledgers = [GPUState(k,devices[k]) for k in plan["fresh_test_models"]]
    save_json(out/"unknown-ranking-demo.json",{
        "resource_request":{"memory_mib":8192,"compute_units":40},
        "ranking":conservative_ranking(model,devices,ledgers,PodRequest("training",ResourceRequest(8192,40))),
        "impossible_request_filter":conservative_ranking(model,devices,ledgers,PodRequest("training",ResourceRequest(999999,40))),
        "data_kind":"offline synthetic model/ranking demonstration; no GPU workload"})
    extra = ["", "## 补充实验与验证文件", "",
             "- [同架构Ampere报告](A_ampere/report.md) / [同架构Ada报告](A_ada/report.md)",
             "- [跨架构混合训练报告](B_multi_architecture/report.md) / [严格Blackwell架构外推](C_blackwell_holdout/report.md)",
             "- [A10历史诊断](D_a10_diagnostic/report.md) / [特征消融分析](ablation-report.md)",
             "- [新型号有效暂定预测覆盖](fresh-unknown-coverage.json) / [未知GPU保守排序](unknown-ranking-demo.json)"]
    if args.pytest_xml:
        suites = list(ET.parse(args.pytest_xml).getroot().iter("testsuite"))
        tests = {k:sum(int(s.get(k,0)) for s in suites) for k in ("tests","errors","failures","skipped")}
        tests["time_s"] = sum(float(s.get("time",0)) for s in suites)
        save_json(out/"pytest-results.json",tests)
        shutil_needed = args.pytest_xml.resolve() != (out/"pytest-results.xml").resolve()
        if shutil_needed:
            (out/"pytest-results.xml").write_bytes(args.pytest_xml.read_bytes())
        extra += ["",f"实际pytest：{tests['tests']}项，errors={tests['errors']}，failures={tests['failures']}，skipped={tests['skipped']}。原文见[pytest结果](pytest-results.txt)。"]
    original = (out/"report.md").read_text(encoding="utf-8").split("\n## 补充实验与验证文件")[0]
    (out/"report.md").write_text(original+"\n".join(extra)+"\n",encoding="utf-8")
    print(json.dumps({"supplemental_reports_written":True,"fresh_coverage":fresh_stats},ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
