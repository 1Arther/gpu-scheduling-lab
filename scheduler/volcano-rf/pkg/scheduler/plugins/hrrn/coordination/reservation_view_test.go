package coordination

import (
	"context"
	"testing"
)

func TestReservationViewReportsOnlyUnreleasedReservations(t *testing.T) {
	ctx := context.Background()
	d, client, _ := durableFixture(t, Resources{10, 10, 1})
	a := plan("a", "node-0", allocation("worker", gpu0, 40, 40, 1))
	b := plan("b", "node-0", allocation("worker", gpu0, 40, 40, 1))
	if err := d.Reserve(ctx, JobPlan{UID: "job", Pods: []PodPlan{b, a}}); err != nil {
		t.Fatal(err)
	}
	v, err := d.ReservationView(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if v.UID != d.UID() || v.ResourceVersion == "" || v.Epoch != d.Epoch() || v.Quarantined || v.Charged[gpu0] != (Resources{80, 80, 2}) || len(v.Members) != 2 || v.Members[0].PodUID != "a" {
		t.Fatalf("invalid reservation-only view: %+v", v)
	}
	v.Members[0].Allocations[0].Resources.Cores = 99
	v.Charged[gpu0] = Resources{}
	view2, err := d.ReservationView(ctx)
	if err != nil || view2.Members[0].Allocations[0].Resources.Cores != 40 || view2.Charged[gpu0].Cores != 80 {
		t.Fatalf("view exposed mutable state: %+v %v", view2, err)
	}
	if _, err = d.Cancel(ctx, "job", "a", "node-0"); err != nil {
		t.Fatal(err)
	}
	view3, err := d.ReservationView(ctx)
	if err != nil || view3.ResourceVersion == v.ResourceVersion || view3.Charged[gpu0].Cores != 40 || len(view3.Members) != 1 {
		t.Fatalf("cancellation not reflected: %+v %v", view3, err)
	}
	newD, err := TakeoverDurable(ctx, client, "test-ns", "ledger", d.UID(), d.Epoch())
	if err != nil {
		t.Fatal(err)
	}
	view4, err := newD.ReservationView(ctx)
	if err != nil || !view4.Quarantined || view4.Epoch == v.Epoch || view4.Charged[gpu0].Cores != 40 {
		t.Fatalf("takeover not reflected: %+v %v", view4, err)
	}
	if _, err := d.ReservationView(ctx); err == nil {
		t.Fatal("old epoch read accepted")
	}
}

func TestReservationViewDoesNotReleaseUncertainPod(t *testing.T) {
	ctx := context.Background()
	d, client, _ := durableFixture(t, Resources{})
	a := plan("a", "node-0", allocation("worker", gpu0, 40, 40, 1))
	if err := d.Reserve(ctx, JobPlan{UID: "job", Pods: []PodPlan{a}}); err != nil {
		t.Fatal(err)
	}
	if _, err := d.Begin(ctx, "job", "a", "node-0"); err != nil {
		t.Fatal(err)
	}
	next, err := TakeoverDurable(ctx, client, "test-ns", "ledger", d.UID(), d.Epoch())
	if err != nil {
		t.Fatal(err)
	}
	view, err := next.ReservationView(ctx)
	if err != nil || !view.Quarantined || len(view.Members) != 1 || view.Members[0].Stage != Uncertain || view.Charged[gpu0].Cores != 40 {
		t.Fatalf("uncertain capacity lost: %+v %v", view, err)
	}
}
