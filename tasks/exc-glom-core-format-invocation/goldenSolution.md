# Golden solution -- exc-glom-core-format-invocation

**Task:** Implement glom.core.format_invocation
**Source:** excision
**Target symbol:** `glom.core.format_invocation`
**Detector:** `glom/core.py:582`

## Why this is the correct fix

The reference answer is the implementation that was removed: the code shipping in glom/core.py at the delivered commit. It is correct by construction -- it is the implementation the repository's own 26 covering tests were written against, and those tests pass against it and fail against the stub.

Note that the verifier accepts any behaviourally equivalent implementation; the diff below shows the original only as the reference point.

## Verified behaviours

The verifier grades 26 case(s):

  - `glom/test/test_basic.py::test_coalesce`
  - `glom/test/test_basic.py::test_invoke`
  - `glom/test/test_check.py::test_check_basic`
  - `glom/test/test_error.py::test_all_public_errors`
  - `glom/test/test_error.py::test_coalesce_stack`
  - `glom/test/test_grouping.py::test_agg`
  - `glom/test/test_path_and_t.py::test_path_access_error_message`
  - `glom/test/test_path_and_t.py::test_path_t_roundtrip`
  - `glom/test/test_path_and_t.py::test_t_picklability`
  - `glom/test/test_reduction.py::test_flatten`
  - `glom/test/test_reduction.py::test_fold`
  - `glom/test/test_reduction.py::test_sum_integers`
  - `glom/test/test_scope_vars.py::test_let`
  - `glom/test/test_scope_vars.py::test_s_scope_assign`
  - `glom/test/test_scope_vars.py::test_vars`
  - `glom/test/test_streaming.py::test_all`
  - `glom/test/test_streaming.py::test_faulty_iterate`
  - `glom/test/test_streaming.py::test_filter`
  - `glom/test/test_streaming.py::test_first`
  - `glom/test/test_streaming.py::test_map`
  - `glom/test/test_streaming.py::test_slice`
  - `glom/test/test_streaming.py::test_split_flatten`
  - `glom/test/test_streaming.py::test_unique`
  - `glom/test/test_streaming.py::test_windowed`
  - `tests_generated/test_doctests_glom_core.py::test_doctest_TType`

## Diff

```diff
--- a/glom/core.py
+++ b/glom/core.py
@@ -591,9 +591,23 @@
     kw_func(a=1, b=2)
 
     """
-    raise NotImplementedError(
-        "glom.core.format_invocation: This function's body was removed for a benchmark task. Implement it so that it satisfies the contract in its signature and docstring."
-    )
+    _repr = kw.pop('repr', bbrepr)
+    if kw:
+        raise TypeError('unexpected keyword args: %r' % ', '.join(kw.keys()))
+    kwargs = kwargs or {}
+    a_text = ', '.join([_repr(a) for a in args])
+    if isinstance(kwargs, dict):
+        kwarg_items = [(k, kwargs[k]) for k in sorted(kwargs)]
+    else:
+        kwarg_items = kwargs
+    kw_text = ', '.join([f'{k}={_repr(v)}' for k, v in kwarg_items])
+
+    all_args_text = a_text
+    if all_args_text and kw_text:
+        all_args_text += ', '
+    all_args_text += kw_text
+
+    return f'{name}({all_args_text})'
 
 
 class Path:
```
