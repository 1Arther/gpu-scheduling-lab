// Copyright 2026 The Volcano Authors. Apache-2.0.
package vgpu

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"sync"
	"testing"

	jsonpatch "gopkg.in/evanphx/json-patch.v4"
	v1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
)

// This test server evaluates actual RFC6902 test/add operations via the JSON
// patch library. It models one Pod, not a Kubernetes apiserver, kubelet, binder,
// node lock or scheduler ledger.
type podCASTestServer struct {
	mu           sync.Mutex
	pod          *v1.Pod
	beforePatch  func(*v1.Pod)
	dropResponse bool
	patches      int
}

func (s *podCASTestServer) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	defer s.mu.Unlock()
	w.Header().Set("Content-Type", "application/json")
	if r.URL.Path != "/api/v1/namespaces/ns/pods/pod" {
		http.Error(w, "wrong path", http.StatusNotFound)
		return
	}
	switch r.Method {
	case http.MethodGet:
		_ = json.NewEncoder(w).Encode(s.pod)
	case http.MethodPatch:
		s.patches++
		if r.Header.Get("Content-Type") != "application/json-patch+json" {
			http.Error(w, "not JSON patch", http.StatusUnsupportedMediaType)
			return
		}
		data, err := io.ReadAll(r.Body)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		if s.beforePatch != nil {
			s.beforePatch(s.pod)
			s.beforePatch = nil
		}
		old, err := json.Marshal(s.pod)
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		patch, err := jsonpatch.DecodePatch(data)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		updated, err := patch.Apply(old)
		if err != nil {
			http.Error(w, err.Error(), http.StatusUnprocessableEntity)
			return
		}
		var pod v1.Pod
		if err := json.Unmarshal(updated, &pod); err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		pod.ResourceVersion = "next-rv"
		s.pod = &pod
		if s.dropResponse {
			http.Error(w, "committed but response lost", http.StatusGatewayTimeout)
			return
		}
		_ = json.NewEncoder(w).Encode(s.pod)
	default:
		http.Error(w, "unsupported", http.StatusMethodNotAllowed)
	}
}

func podCASFixture(t *testing.T) (*podCASTestServer, kubernetes.Interface, *v1.Pod) {
	t.Helper()
	pod := &v1.Pod{TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "Pod"}, ObjectMeta: metav1.ObjectMeta{
		Namespace: "ns", Name: "pod", UID: types.UID("expected-uid"), ResourceVersion: "rv-1",
		Annotations: map[string]string{"keep/existing": "yes"}, Labels: map[string]string{"keep": "label"}},
		Spec: v1.PodSpec{Containers: []v1.Container{{Name: "worker", Image: "local-test"}}}}
	s := &podCASTestServer{pod: pod.DeepCopy()}
	server := httptest.NewServer(s)
	t.Cleanup(server.Close)
	client, err := kubernetes.NewForConfig(&rest.Config{Host: server.URL})
	if err != nil {
		t.Fatal(err)
	}
	return s, client, pod
}

func TestCoordinatedPodPatchPreservesFields(t *testing.T) {
	s, client, pod := podCASFixture(t)
	beforeSpec := pod.Spec.DeepCopy()
	updated, err := PatchPodAnnotationsIfUnchanged(context.Background(), client, pod, map[string]string{"gpu.test/id": "GPU-1", "gpu.test/quota": "40"})
	if err != nil {
		t.Fatal(err)
	}
	if s.patches != 1 || updated.UID != pod.UID || updated.ResourceVersion != "next-rv" || updated.Annotations["keep/existing"] != "yes" || updated.Annotations["gpu.test/id"] != "GPU-1" || updated.Labels["keep"] != "label" || !reflect.DeepEqual(updated.Spec, *beforeSpec) {
		t.Fatalf("unintended Pod mutation: %+v", updated)
	}
	if pod.Annotations["gpu.test/id"] != "" || pod.ResourceVersion != "rv-1" {
		t.Fatal("caller Pod snapshot was mutated")
	}
}

func TestCoordinatedPodPatchInitializesAnnotations(t *testing.T) {
	s, client, pod := podCASFixture(t)
	s.pod.Annotations = nil
	pod.Annotations = nil
	updated, err := PatchPodAnnotationsIfUnchanged(context.Background(), client, pod, map[string]string{"gpu.test/id": "GPU-1"})
	if err != nil || updated.Annotations["gpu.test/id"] != "GPU-1" {
		t.Fatalf("cannot initialize annotations: updated=%+v err=%v", updated, err)
	}
}

func TestCoordinatedPodPatchRejectsSameNameReplacementAndStaleVersion(t *testing.T) {
	for _, test := range []struct {
		name    string
		change  func(*v1.Pod)
		preRead bool
	}{
		{"replacement-before-read", func(p *v1.Pod) { p.UID = "replacement"; p.ResourceVersion = "rv-2" }, true},
		{"replacement-after-read", func(p *v1.Pod) { p.UID = "replacement"; p.ResourceVersion = "rv-2" }, false},
		{"replacement-after-read-same-rv", func(p *v1.Pod) { p.UID = "replacement" }, false},
		{"same-uid-stale-rv-before-read", func(p *v1.Pod) { p.ResourceVersion = "rv-2" }, true},
		{"same-uid-stale-rv", func(p *v1.Pod) { p.ResourceVersion = "rv-2"; p.Annotations["concurrent"] = "preserve" }, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			s, client, pod := podCASFixture(t)
			if test.preRead {
				test.change(s.pod)
			} else {
				s.beforePatch = test.change
			}
			_, err := PatchPodAnnotationsIfUnchanged(context.Background(), client, pod, map[string]string{"gpu.test/id": "GPU-1"})
			if err == nil || s.pod.Annotations["gpu.test/id"] != "" {
				t.Fatalf("stale UID/RV was accepted: err=%v pod=%+v", err, s.pod)
			}
			if test.preRead && s.patches != 0 {
				t.Fatal("stale Pod was patched after GET")
			}
			if !test.preRead && s.patches != 1 {
				t.Fatal("expected atomic test-and-patch attempt")
			}
		})
	}
}

func TestCoordinatedPodPatchFailureOrAmbiguousResponseNeverRetries(t *testing.T) {
	s, client, pod := podCASFixture(t)
	s.dropResponse = true // actual server write occurred; response says timeout
	_, err := PatchPodAnnotationsIfUnchanged(context.Background(), client, pod, map[string]string{"gpu.test/id": "GPU-1"})
	if err == nil || s.patches != 1 || s.pod.Annotations["gpu.test/id"] != "GPU-1" {
		t.Fatalf("must report unknown outcome without retry: err=%v attempts=%d", err, s.patches)
	}
}

func TestCoordinatedPodPatchRequiresUIDAndResourceVersion(t *testing.T) {
	for _, field := range []string{"uid", "rv", "namespace", "name", "annotations"} {
		t.Run(field, func(t *testing.T) {
			s, client, pod := podCASFixture(t)
			annotations := map[string]string{"gpu.test/id": "GPU-1"}
			switch field {
			case "uid":
				pod.UID = ""
			case "rv":
				pod.ResourceVersion = ""
			case "namespace":
				pod.Namespace = ""
			case "name":
				pod.Name = ""
			case "annotations":
				annotations = nil
			default:
				t.Fatal(fmt.Sprintf("unknown case %s", field))
			}
			if _, err := PatchPodAnnotationsIfUnchanged(context.Background(), client, pod, annotations); err == nil || s.patches != 0 {
				t.Fatal("incomplete identity was accepted")
			}
		})
	}
}
