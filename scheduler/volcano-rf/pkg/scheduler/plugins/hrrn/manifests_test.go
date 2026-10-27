package hrrn

import (
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"testing"

	"gopkg.in/yaml.v2"
	kubeyaml "sigs.k8s.io/yaml"
	batch "volcano.sh/apis/pkg/apis/batch/v1alpha1"
	"volcano.sh/volcano/pkg/scheduler/conf"
)

func TestPreparedConfigsAndJobsUseNativeGoTypes(t *testing.T) {
	dir := os.Getenv("HRRN_MANIFESTS")
	if dir == "" {
		t.Skip("set HRRN_MANIFESTS to prepared stage4 manifests")
	}
	files, err := filepath.Glob(filepath.Join(dir, "configs", "*.yaml"))
	if err != nil || len(files) != 5 {
		t.Fatal("five configs required", err)
	}
	var baseline []conf.Tier
	for _, file := range files {
		data, err := os.ReadFile(file)
		if err != nil {
			t.Fatal(err)
		}
		var cfg conf.SchedulerConfiguration
		if err = yaml.UnmarshalStrict(data, &cfg); err != nil {
			t.Fatal(file, err)
		}
		if cfg.Actions != "enqueue, allocate, backfill" || !safeOrder(cfg.Tiers) || !safeCompleteOrder(cfg.Tiers) {
			t.Fatal("invalid actions or comparator precedence", file)
		}
		for ti := range cfg.Tiers {
			for pi := range cfg.Tiers[ti].Plugins {
				op := &cfg.Tiers[ti].Plugins[pi]
				if op.Name == PluginName {
					op.Arguments["enabled"] = false
					op.Arguments["complete-job"] = false
				}
				if op.Name == "deviceshare" {
					op.Arguments["deviceshare.RFEnable"] = true
					op.Arguments["deviceshare.GPUSelectPolicy"] = "rf-job"
				}
			}
		}
		if baseline == nil {
			baseline = cfg.Tiers
		} else if !reflect.DeepEqual(baseline, cfg.Tiers) {
			t.Fatal("uncontrolled configuration difference", file)
		}
	}
	files, err = filepath.Glob(filepath.Join(dir, "jobs", "*.yaml"))
	if err != nil || len(files) != 6 {
		t.Fatal("six Jobs required", err)
	}
	for _, file := range files {
		data, _ := os.ReadFile(file)
		var job batch.Job
		if err := kubeyaml.UnmarshalStrict(data, &job); err != nil {
			t.Fatal(file, err)
		}
		total := 0
		for _, task := range job.Spec.Tasks {
			total += int(task.Replicas)
			for _, container := range task.Template.Spec.Containers {
				if len(container.Command) != 3 || container.Command[0] != "/bin/sh" || container.Command[1] != "-c" {
					t.Fatal("unexpected workload", file)
				}
			}
		}
		if job.Annotations[TotalPodsAnnotation] != strconv.Itoa(total) || job.Annotations[EstimateAnnotation] == "" {
			t.Fatal("missing Job metadata", file)
		}
		if job.Spec.SchedulerName != "volcano-hrrn-lab" {
			t.Fatal("wrong scheduler", file)
		}
	}
}
