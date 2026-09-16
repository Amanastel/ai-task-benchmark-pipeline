# Golden solution -- exc-glom-core-format-target-spec-trace

**Task:** Implement glom.core.format_target_spec_trace
**Source:** excision
**Target symbol:** `glom.core.format_target_spec_trace`
**Detector:** `glom/core.py:243`

## Why this is the correct fix

The reference answer is the implementation that was removed: the code shipping in glom/core.py at the delivered commit. It is correct by construction -- it is the implementation the repository's own 28 covering tests were written against, and those tests pass against it and fail against the stub.

Note that the verifier accepts any behaviourally equivalent implementation; the diff below shows the original only as the reference point.

## Verified behaviours

The verifier grades 28 case(s):

  - `glom/test/test_check.py::test_check_basic`
  - `glom/test/test_check.py::test_check_multi`
  - `glom/test/test_cli.py::test_main_basic`
  - `glom/test/test_error.py::test_3_11_byte_code_caret`
  - `glom/test/test_error.py::test_all_public_errors`
  - `glom/test/test_error.py::test_branching_stack`
  - `glom/test/test_error.py::test_coalesce_stack`
  - `glom/test/test_error.py::test_glom_dev_debug`
  - `glom/test/test_error.py::test_glom_error_double_stack`
  - `glom/test/test_error.py::test_glom_error_stack`
  - `glom/test/test_error.py::test_long_target_repr`
  - `glom/test/test_error.py::test_midway_branch`
  - `glom/test/test_error.py::test_nesting_stack`
  - `glom/test/test_error.py::test_pae_scope_printable`
  - `glom/test/test_error.py::test_partially_failing_branch`
  - `glom/test/test_error.py::test_regular_error_stack`
  - `glom/test/test_error.py::test_unicode_stack`
  - `glom/test/test_match.py::test_check_ported_tests`
  - `glom/test/test_mutation.py::test_bad_assign_target`
  - `glom/test/test_mutation.py::test_bad_delete_target`
  - `glom/test/test_mutation.py::test_sequence_assign`
  - `glom/test/test_mutation.py::test_sequence_delete`
  - `glom/test/test_mutation.py::test_unregistered_assign`
  - `glom/test/test_mutation.py::test_unregistered_delete`
  - `glom/test/test_path_and_t.py::test_path_access_error_message`

## Diff

```diff
--- a/glom/core.py
+++ b/glom/core.py
@@ -246,9 +246,44 @@
     """
     unpack a scope into a multi-line but short summary
     """
-    raise NotImplementedError(
-        "glom.core.format_target_spec_trace: This function's body was removed for a benchmark task. Implement it so that it satisfies the contract in its signature and docstring."
+    segments = []
+    indent = " " + "|" * depth
+    tick = "| " if depth else "- "
+
+    def mk_fmt(label, t=None):
+        pre = indent + (t or tick) + label + ": "
+        fmt_width = width - len(pre)
+        return lambda v: pre + _format_trace_value(v, fmt_width)
+
+    fmt_t = mk_fmt("Target")
+    fmt_s = mk_fmt("Spec")
+    fmt_b = mk_fmt("Spec", "+ ")
+    recurse = lambda s, last=False: format_target_spec_trace(
+        s, root_error, width, depth + 1, prev_target, last
     )
+    tb_exc_line = lambda e: "".join(traceback.format_exception_only(type(e), e))[:-1]
+    fmt_e = lambda e: indent + tick + tb_exc_line(e)
+    for scope, spec, target, error, branches in _unpack_stack(scope):
+        if target is not prev_target:
+            segments.append(fmt_t(target))
+        prev_target = target
+        if branches:
+            segments.append(fmt_b(spec))
+            segments.extend([recurse(s) for s in branches[:-1]])
+            segments.append(recurse(branches[-1], last_branch))
+        else:
+            segments.append(fmt_s(spec))
+        if error is not None and error is not root_error:
+            last_line_error = True
+            segments.append(fmt_e(error))
+        else:
+            last_line_error = False
+    if depth:  # \ on first line, X on last line
+        remark = lambda s, m: s[: depth + 1] + m + s[depth + 2 :]
+        segments[0] = remark(segments[0], "\\")
+        if not last_branch or last_line_error:
+            segments[-1] = remark(segments[-1], "X")
+    return "\n".join(segments)
 
 
 # TODO: not used (yet)
```
