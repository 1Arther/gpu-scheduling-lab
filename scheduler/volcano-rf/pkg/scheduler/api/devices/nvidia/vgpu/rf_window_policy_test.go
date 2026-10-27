package vgpu

// Offline CEL validation only. Does not install a policy or contact API Server.
import (
	"encoding/json"
	"os"
	"testing"

	"github.com/google/cel-go/cel"
)

func TestRFLogicalWindowPolicyCEL(t *testing.T) {
	path := os.Getenv("RF_WINDOW_CASES")
	if path == "" {
		t.Skip("set RF_WINDOW_CASES to generated policy-cases.json")
	}
	var fixtures struct {
		Variables  []struct{ Name, Expression string }
		Expression string
		Cases      []struct {
			Name    string
			Pod     map[string]interface{}
			Allowed bool
		}
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if err = json.Unmarshal(data, &fixtures); err != nil {
		t.Fatal(err)
	}
	env, err := cel.NewEnv(cel.Variable("object", cel.DynType), cel.Variable("variables", cel.MapType(cel.StringType, cel.DynType)))
	if err != nil {
		t.Fatal(err)
	}
	compile := func(expr string) cel.Program {
		ast, issues := env.Compile(expr)
		if issues.Err() != nil {
			t.Fatal(issues.Err())
		}
		program, e := env.Program(ast)
		if e != nil {
			t.Fatal(e)
		}
		return program
	}
	variables := make([]cel.Program, len(fixtures.Variables))
	for i, v := range fixtures.Variables {
		variables[i] = compile(v.Expression)
	}
	validation := compile(fixtures.Expression)
	for _, c := range fixtures.Cases {
		t.Run(c.Name, func(t *testing.T) {
			values := map[string]interface{}{}
			for i, v := range fixtures.Variables {
				value, _, e := variables[i].Eval(map[string]interface{}{"object": c.Pod, "variables": values})
				if e != nil {
					t.Fatal(e)
				}
				values[v.Name] = value
			}
			value, _, e := validation.Eval(map[string]interface{}{"object": c.Pod, "variables": values})
			if e != nil {
				t.Fatal(e)
			}
			if value.Value() != c.Allowed {
				t.Fatalf("allowed=%v expected=%v", value.Value(), c.Allowed)
			}
		})
	}
}
