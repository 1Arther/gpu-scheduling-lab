"""Execute fifth-round integration demonstrations with frozen artifacts only."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import platform
import re
import shutil
import time

import numpy as np

from ..comparison import write_csv
from ..dataset import write_jsonl
from ..execution import ExecutionModel
from ..fleet import create_fleet
from ..profiles import ROOT, load_profiles
from ..run import save_json, sha256
from ..simulation import simulate
from ..workloads import generate_trace
from .adapters import MockVolcanoAdapter, RuntimeScheduler
from .bundle import DEFAULT_BUNDLE, freeze
from .pods import DeviceRequest, PodSpec, parse_pod
from .pool import GPUPool
from .schema import make_fleet
from .service import SuitabilityService


def historical_snapshot():
    return {str(f.relative_to(ROOT)):sha256(f) for d in sorted((ROOT/"results").glob("offline-v[1234]*")) if d.is_dir()
            for f in sorted(d.rglob("*")) if f.is_file()}


def source_audit(out):
    repository = ROOT.parent
    roots = [repository/"audit/custom-volcano",repository/"audit/scheduler-source"]
    files = []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.go")):
            lines = path.read_text(encoding="utf-8").splitlines()
            functions = [{"line":i+1,"declaration":line.strip()} for i,line in enumerate(lines) if re.match(r"^func\s",line)]
            files.append({"path":str(path.relative_to(repository)),"sha256":sha256(path),"functions":functions})
    result = {"local_source_files":files,"complete_Go_checkout":False,"go_mod_found":any(root.exists() and list(root.rglob("go.mod")) for root in roots),
              "missing_definitions":["GPUDevices/GPUDevice complete implementation","Allocate","addResource/subResource","HAMi sharing AddPod/TryAddPod implementation"],
              "version_proof":"local custom source fragments only; no tagged/compiled v1.13.1 checkout or proof of correspondence to current binary",
              "cluster_access_performed":False,"old_DQN_code_used_for_new_policy":False}
    save_json(out/"volcano-local-source-audit.json",result)
    return result


def dynamic_demo(service):
    fleet = make_fleet(service.catalog)
    pool = GPUPool(service,fleet)
    target = next(g["uuid"] for g in fleet if g["device_id"] == "l40")
    capacity = next(g["hardware"]["memory_capacity_mib"] for g in fleet if g["uuid"] == target)
    events = []
    followups = [parse_pod(ROOT/"examples/runtime/inference.yaml"),
                 PodSpec("followup-training","followup-training","offline","training","CUDA",(DeviceRequest("workload",1,8192,None,20),),None,{}, {})]
    def record(event):
        snapshots = pool.snapshot()
        current = next(g for g in snapshots if g["uuid"] == target)
        events.append({"event":event,"gpu_uuid":target,"snapshot":current,"classification":service.classify_gpu(current),
                       "followup_rankings":{p.task_type:service.rank_gpus(p,snapshots) for p in followups}})
    def put(uid,task,mem,cores):
        p = PodSpec(uid,uid,"offline",task,"CUDA",(DeviceRequest("workload",1,mem,None,cores),),None,{}, {})
        scoped = [g for g in pool.snapshot() if g["uuid"] == target]
        selection = service.rank_gpus(p,scoped)["recommendation"]
        if selection is None:
            raise AssertionError("Constructed dynamic software demo no longer fits")
        held = pool.reserve(p,selection)
        pool.commit(held["reservation_id"],[target])
    record("initial")
    put("compute-heavy","training",1000,60); record("allocate_compute_heavy")
    put("memory-heavy","inference",capacity-7000,5); record("allocate_memory_heavy")
    pool.release("compute-heavy"); record("release_compute_heavy")
    pool.release("memory-heavy"); record("release_all")
    assert len({json.dumps(e["snapshot"]["hardware"],sort_keys=True) for e in events}) == 1
    assert events[0]["snapshot"]["dynamic"]["free_memory_mib"] == events[-1]["snapshot"]["dynamic"]["free_memory_mib"]
    return events


def simulation_regression(service,out):
    profiles,trace = load_profiles(),generate_trace("bursty_mixed",101,60)
    summaries,audits = [],{}
    for name in ("binpack","static_rf","dynamic_rf"):
        fleet = create_fleet(profiles)
        scheduler = RuntimeScheduler(name,service)
        result = simulate(trace,profiles,scheduler,ExecutionModel(seed=707),fleet=fleet)
        for e in result["events"]:
            if "state_after" in e:
                s = e["state_after"]
                assert 0 <= s["remaining_memory_mib"] <= s["capacity_memory_mib"]
                assert 0 <= s["remaining_compute_units"] <= 100
                assert 0 <= s["allocated_instances"] <= 10
        assert result["metrics"]["completed_total_after_drain"] == 60
        assert all(not s.allocations and s.remaining_compute_units == 100 and s.remaining_memory_mib == s.profile.modeled_memory_mib for s in fleet.values())
        audits[name] = {"completed":60,"all_resources_restored":True,"no_overallocation":True,"api_rank_calls":scheduler.calls,
                       "classification_updates":sum(e.get("state_prediction_after") is not None for e in result["events"])}
        summaries.append(result["metrics"])
        write_jsonl(out/f"simulation-{name}-events.jsonl",result["events"])
        write_csv(out/f"simulation-{name}-pods.csv",result["pods"])
    assert len({r["trace_sha256"] for r in summaries}) == 1
    save_json(out/"simulation-regression.json",{"metrics":summaries,"audit":audits,"timing_model":"unchanged independent synthetic execution model, NOT classification contract"})
    write_csv(out/"simulation-regression.csv",summaries)
    return audits


def run(output=None,bundle=DEFAULT_BUNDLE):
    out = Path(output or ROOT/"results/offline-v5").resolve()
    if out.exists() and any(out.iterdir()):
        raise ValueError("Use a new output directory, historical results will not be overwritten")
    if any(d.resolve() == out or d.resolve() in out.parents for d in (ROOT/"results").glob("offline-v[1234]*")):
        raise ValueError("Cannot write into historical rounds")
    out.mkdir(parents=True,exist_ok=True)
    before,start = historical_snapshot(),time.perf_counter()
    manifest = freeze(bundle)
    service = SuitabilityService(bundle)
    if service.model_error:
        raise ValueError(service.model_error)
    fleet = make_fleet(service.catalog)
    save_json(out/"gpu-list.json",fleet)
    save_json(out/"gpu-one.json",fleet[0])
    save_json(out/"frozen-model-manifest.json",manifest)
    save_json(out/"initial-classifications.json",[service.classify_gpu(g) for g in fleet])
    choices = {}
    for name in ("training","inference","unlabelled"):
        pod = parse_pod(ROOT/f"examples/runtime/{name}.yaml")
        ranking = service.rank_gpus(pod,fleet)
        save_json(out/f"demo-{name}.json",ranking)
        choices[name] = ranking["recommendation"]
        print(name,ranking["recommendation"]["gpu_uuids"] if ranking["recommendation"] else ranking["status"],flush=True)
    dynamic = dynamic_demo(service)
    save_json(out/"demo-dynamic.json",dynamic)
    failure = SuitabilityService(bundle,force_fallback=True)
    save_json(out/"demo-model-failure.json",failure.rank_gpus(parse_pod(ROOT/"examples/runtime/inference.yaml"),fleet))
    class TimeoutAdviser:
        def rank_gpus(self,*args):
            raise TimeoutError("injected_advisory_timeout_no_network")
    pool = GPUPool(service,fleet)
    bridge = MockVolcanoAdapter(service,pool,adviser=TimeoutAdviser())
    pod = parse_pod(ROOT/"examples/runtime/inference.yaml")
    plan = bridge.plan(pod)
    allocated = bridge.allocate(pod,plan)
    released = pool.release(pod.uid)
    save_json(out/"demo-volcano-mock.json",{"timeout_plan":plan,"allocation":allocated,"release":released,"Go_plugin_compiled_or_deployed":False})
    audits = simulation_regression(service,out)
    elapsed = []
    for _ in range(15):
        t = time.perf_counter(); service.rank_gpus(pod,fleet); elapsed.append((time.perf_counter()-t)*1000)
    save_json(out/"inference-overhead.json",{"warm_rank_11_GPU_ms":{"mean":float(np.mean(elapsed)),"p95":float(np.percentile(elapsed,95))},
               "actual_RF_calls_including_demos_and_regression":service.inference_calls,"model_loads_for_default_service_instance":1,
               "sample_count":len(elapsed),"scope":"local CPU process with bounded memoization, not RPC or GPU execution benchmark"})
    source = source_audit(out)
    assert before == historical_snapshot()
    save_json(out/"historical-results-preserved.json",{"unchanged":True,"files":len(before),"sha256":before})
    sources = sorted((ROOT/"gpu_lab/runtime").glob("*.py"))+[ROOT/"tests/test_round5.py"]
    save_json(out/"run-manifest.json",{"python":platform.python_version(),"model_version":service.version,"elapsed_s":time.perf_counter()-start,
               "source_sha256":{str(f.relative_to(ROOT)):sha256(f) for f in sources if f.exists()},"training_calls":0,"cluster_access":False})
    lines = ["# 第五轮：可复用 Pod–GPU 分类与选择接口", "",
             f"默认版本 `{service.version}`，冻结第四轮known_states模型，未重新训练。4090/5090/L40均在拟合型号和数值覆盖中，初始状态相关验证通过。选择依据见 [模型清单](frozen-model-manifest.json)，不是按最高测试准确率选择。", "",
             "所有分类仅表示合成FP32参考合同适配性，不是真实GPU性能保证。GPU UUID和节点使用SIM前缀的离线身份，不是本次查询的真实设备。", "",
             "## 已执行的演示", "", "| 场景 | 推荐节点 | UUID | 原因 |", "|---|---|---|---|"]
    for name,rec in choices.items():
        lines.append(f"| {name} | {rec['node'] if rec else '-'} | {','.join(rec['gpu_uuids']) if rec else '-'} | {'ordinary binpack: no type inference' if name == 'unlabelled' else rec['reason'] if rec else 'no fit'} |")
    lines += ["", "training示例请求36864MiB/50配额，仅L40满足显存；inference示例请求4096MiB/20配额，比较所有11卡的推理提示、带宽、共享、装箱与平衡因素。候选、过滤原因、RF概率与实际排序分数都在JSON中。", "",
              "| 动态事件 | 剩余显存MiB | 剩余配额 | 实例 | 综合分类 | 状态 |", "|---|---|---|---|---|---|"]
    for row in dynamic:
        d,c = row["snapshot"]["dynamic"],row["classification"]
        lines.append(f"| {row['event']} | {d['free_memory_mib']} | {d['free_compute_quota']} | {d['allocated_vgpu_count']} | {c['label_zh']} | {c['prediction_status']} |")
    lines += ["", "每步均重新进入分类接口并返回新状态的分类；相同特征允许命中进程内缓存。静态硬件不随分配改变。每步还对完整11卡重新排列后续训练/推理Pod。", "",
              "## 接口与安全边界", "",
              "`gpu_lab.runtime.classify_gpu(snapshot)` 与 `rank_gpus(pod,gpus)` 为稳定入口；也可持有SuitabilityService一次加载模型反复调用。dict/JSON是交互边界，Pod支持mapping/YAML文本/Path。资源缺失、非法Label、无效GPU输入分别明确报错或回退。", "",
              "先执行后端、健康、插件管理、配额语义、显存、算力、实例、100-core独占、节点与卡型条件检查，再打分。四分类不会批准超额请求，也不会拒绝所有比参考负载更小的Pod。未知/低可信型号和模型故障使用保守理论排序，字段ranking_basis、rf_probability、fallback_score分开，不将回退分数伪装为概率。", "",
              "Pod无Label但有vGPU请求时使用普通装箱，保留unknown类型，绝不默认为training/inference；无vGPU请求时交回普通调度器。非法Label明确错误。", "",
              "解析兼容本地自定义片段的limits触发、memory/cores从requests补取、memory-only默认一张、无显存默认100%、cores默认0。0核是无保证限额语义，不能当作已预留算力；存在该类实例时RF只作诊断。资源字段使用整数，offline约定MiB；真实插件MB/MiB转换仍需在完整源码/配置确认后接入。", "",
              "vgpu-number>1在单个container内选同一节点的不同物理GPU；跨container可以在资源允许时共用一张GPU。计划是确定性的贪心可行方案，不保证求出所有可能的多容器组合；失败时安全交回原调度器/重试。RF旧输入计数仍按vGPU实例，独立Pod计数另存running_*_pod_count。", "",
              "GPUPool是进程内模拟账本：RLock+设备revision校验、全计划原子预留、真实UUID结果确认、幂等释放、失败回滚、未提交预留过期回收。它不构成多进程或集群锁。真正集成时Volcano是唯一账本权威，Python只给建议，不双重计账。", "",
              "## 仿真兼容性", ""]
    for name,a in audits.items():
        lines.append(f"- {name}: 完成{a['completed']}个Pod，API选择{a['api_rank_calls']}次，分类更新{a['classification_updates']}次，无超分配，全部资源恢复。")
    lines += ["", "同一60任务序列/seed101与旧独立合成执行模型seed707。未改原仿真器，新增RuntimeScheduler通过choose/on_state_change接入；扣减与释放只由旧仿真器负责。", "",
              "## Volcano集成状态", "",
              f"本地可读Go片段{len(source['local_source_files'])}个，缺go.mod与Allocate/addResource定义。已确认deviceshare过滤/节点排序调用、设备解析/排序/试分配、UUID编码；不能证明是完整v1.13.1定制二进制对应源码。Go插件没有编译或部署。详见 [集成设计](../../docs/runtime-volcano-integration.md) 与 [源码审计](volcano-local-source-audit.json)。", "",
              "推荐后续采用单个常驻本地Python推理服务，Go通过有deadline的JSON请求调用，复用本模块dispatch。当前提供Python API、JSON-lines worker和模拟适配器，没有启动HTTP服务或修改在线DQN配置。异常/超时/版本错误回退确定性策略；评分UUID必须传入最终分配计划，在原账本锁下重校验，不可重新随机选卡。", "",
              "## 产物与复现", "",
              "- [训练Pod演示](demo-training.json) / [推理Pod演示](demo-inference.json) / [动态演示](demo-dynamic.json)",
              "- [缺Label回退](demo-unlabelled.json) / [模型失败回退](demo-model-failure.json) / [Volcano模拟适配器](demo-volcano-mock.json)",
              "- [GPU输入示例](gpu-list.json) / [仿真回归](simulation-regression.json) / [本机API开销](inference-overhead.json)",
              "- [历史结果完整性](historical-results-preserved.json)", "",
              "在项目目录运行：", "", "```powershell", "$env:TEMP=(Join-Path (Get-Location) '.tmp'); $env:TMP=$env:TEMP",
              "$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'", ".\\.venv\\Scripts\\python.exe -m gpu_lab.runtime.cli demo --output results/offline-v5-replay",
              ".\\.venv\\Scripts\\python.exe -m gpu_lab.runtime.cli rank --pod examples/runtime/inference.yaml --gpus-json results/offline-v5-replay/gpu-list.json",
              ".\\.venv\\Scripts\\python.exe -m pytest -q --basetemp .tmp/pytest-round5-replay", "```"]
    (out/"report.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    return {"output":str(out),"model_version":service.version,"historical_files_unchanged":len(before),"training_calls":0}
