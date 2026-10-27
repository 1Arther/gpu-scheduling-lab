"""Publication-exportable plots of explicitly synthetic experiments."""
import os
from pathlib import Path

from .profiles import ROOT

os.environ.setdefault("MPLCONFIGDIR", str(ROOT / ".tmp/matplotlib"))
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

COLORS = {"binpack": "#71717a", "static_rf": "#2563eb", "dynamic_rf": "#e87524"}
NAMES = {"binpack": "Binpack", "static_rf": "Static RF", "dynamic_rf": "Dynamic RF + Pod"}
SCENARIO_NAMES = {"steady_mixed": "Steady mixed", "bursty_mixed": "Bursty mixed", "training_heavy": "Training heavy", "memory_heavy": "Memory heavy"}


def save(fig, output, name):
    for extension in ("png", "svg"):
        fig.savefig(output / f"{name}.{extension}", dpi=180, bbox_inches="tight")
    plt.close(fig)


def make_plots(output, comparison, summary, scenarios, representative):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False,
                         "axes.titleweight": "bold", "figure.facecolor": "white"})
    test = comparison["results"]["test"]
    fig, ax = plt.subplots(figsize=(8, 4.8), constrained_layout=True)
    x = np.arange(3)
    methods = ("manual_rule", "static_rf", "dynamic_rf")
    for shift, metric, color in ((-.19, "accuracy", "#2563eb"), (.19, "macro_f1", "#e87524")):
        bars = ax.bar(x + shift, [test[m][metric] for m in methods], .36, label=metric, color=color)
        ax.bar_label(bars, fmt="%.3f", padding=3)
    ax.set(xticks=x, xticklabels=("Original rule\n(label generator)", "Static RF", "Dynamic RF (saved)"),
           ylim=(0, 1.12), ylabel="Agreement with artificial labels", title="Classification comparison — synthetic labels")
    ax.legend(loc="lower right")
    save(fig, output, "classification-comparison")
    specs = (("mean_wait_s", "Mean Pod wait (s)", 1),
             ("training_mean_jct_s", "Training JCT (s)", 1),
             ("inference_pod_response_p95_s", "Inference batch Pod response P95 (s)", 1),
             ("reserved_compute_utilization", "Reserved compute utilization (%)", 100),
             ("reserved_memory_utilization", "Reserved memory utilization (%)", 100),
             ("completed_within_horizon", "Completed Pods within fixed horizon", 1))
    fig, axes = plt.subplots(3, 2, figsize=(13.8, 11.2), constrained_layout=True)
    for ax, (key, title, multiplier) in zip(axes.flat, specs):
        for i, strategy in enumerate(COLORS):
            rows = [next(r for r in summary if r["scenario"] == scenario and r["strategy"] == strategy and r["interference"] == 1.) for scenario in scenarios]
            values = [r[key + "_mean"] * multiplier for r in rows]
            errors = [r[key + "_std"] * multiplier for r in rows]
            ax.bar(np.arange(len(scenarios)) + (i - 1) * .25, values, .24, yerr=errors, capsize=3,
                   label=NAMES[strategy], color=COLORS[strategy])
        ax.set(title=title, xticks=np.arange(len(scenarios)),
               xticklabels=[SCENARIO_NAMES[s].replace(" ", "\n", 1) for s in scenarios])
        ax.grid(axis="y", alpha=.15)
        ax.set_axisbelow(True)
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="outside lower center", ncol=3)
    fig.suptitle("Offline scheduling simulation — synthetic execution assumptions\nBars: mean across paired trace seeds; error bars: sample standard deviation", fontsize=14)
    save(fig, output, "scheduling-comparison")
    levels = sorted({r["interference"] for r in summary if r["scenario"] == "bursty_mixed"})
    if len(levels) > 1:
        fig, axes = plt.subplots(1, 2, figsize=(11.8, 4.5), constrained_layout=True)
        for ax, key, title in ((axes[0], "training_mean_jct_s", "Training JCT (s)"),
                               (axes[1], "inference_pod_response_p95_s", "Inference batch response P95 (s)")):
            for strategy in COLORS:
                data = [next(r for r in summary if r["scenario"] == "bursty_mixed" and r["strategy"] == strategy and r["interference"] == level) for level in levels]
                ax.errorbar(levels, [r[key+"_mean"] for r in data], yerr=[r[key+"_std"] for r in data],
                            marker="o", capsize=4, color=COLORS[strategy], label=NAMES[strategy])
            ax.set(title=title, xlabel="Synthetic interference multiplier", xticks=levels)
            ax.grid(alpha=.15)
        axes[1].legend(fontsize=9)
        fig.suptitle("Bursty trace sensitivity — same Pods and execution seeds")
        save(fig, output, "interference-sensitivity")
    if representative:
        fig, axes = plt.subplots(3, 1, figsize=(11.5, 8.5), sharex=True, constrained_layout=True)
        for strategy, result in representative.items():
            rows = result["timeline"]
            for ax, key, multiplier in zip(axes, ("reserved_compute_fraction", "reserved_memory_fraction", "queued_pods"), (100, 100, 1)):
                x = [r["start_s"] for r in rows] + [rows[-1]["end_s"], result["metrics"]["observation_horizon_s"]]
                y = [r[key]*multiplier for r in rows] + [0, 0]
                ax.step(x, y, where="post", color=COLORS[strategy], label=NAMES[strategy], linewidth=1.2)
                ax.grid(alpha=.15)
        axes[0].set(ylabel="Reserved compute (%)", title="Representative paired trace — synthetic reservations")
        axes[1].set(ylabel="Reserved memory (%)")
        axes[2].set(ylabel="Queued Pods", xlabel="Simulated time (s)")
        axes[0].legend(ncol=3, fontsize=9)
        save(fig, output, "representative-timeline")
