# Golden solution -- exc-glom-grouping-group

**Task:** Implement glom.grouping.GROUP
**Source:** excision
**Target symbol:** `glom.grouping.GROUP`
**Detector:** `glom/grouping.py:100`

## Why this is the correct fix

The reference answer is the implementation that was removed: the code shipping in glom/grouping.py at the delivered commit. It is correct by construction -- it is the implementation the repository's own 12 covering tests were written against, and those tests pass against it and fail against the stub.

Note that the verifier accepts any behaviourally equivalent implementation; the diff below shows the original only as the reference point.

## Verified behaviours

The verifier grades 12 case(s):

  - `glom/test/test_error.py::test_all_public_errors`
  - `glom/test/test_grouping.py::test_agg`
  - `glom/test/test_grouping.py::test_bucketing`
  - `glom/test/test_grouping.py::test_corner_cases`
  - `glom/test/test_grouping.py::test_limit`
  - `glom/test/test_grouping.py::test_sample`
  - `tests_generated/test_doctests_glom_grouping.py::test_doctest_Avg`
  - `tests_generated/test_doctests_glom_grouping.py::test_doctest_First`
  - `tests_generated/test_doctests_glom_grouping.py::test_doctest_Group`
  - `tests_generated/test_doctests_glom_grouping.py::test_doctest_Limit`
  - `tests_generated/test_doctests_glom_grouping.py::test_doctest_Max`
  - `tests_generated/test_doctests_glom_grouping.py::test_doctest_Min`

## Diff

```diff
--- a/glom/grouping.py
+++ b/glom/grouping.py
@@ -101,9 +101,58 @@
     """
     Group mode dispatcher; also sentinel for current mode = group
     """
-    raise NotImplementedError(
-        "glom.grouping.GROUP: This function's body was removed for a benchmark task. Implement it so that it satisfies the contract in its signature and docstring."
-    )
+    recurse = lambda spec: scope[glom](target, spec, scope)
+    tree = scope[ACC_TREE]  # current accumulator support structure
+    if callable(getattr(spec, "agg", None)):
+        return spec.agg(target, tree)
+    elif callable(spec):
+        return spec(target)
+    _spec_type = type(spec)
+    if _spec_type not in (dict, list):
+        raise BadSpec("Group mode expected dict, list, callable, or aggregator, not: %r" % (spec,))
+    _spec_id = id(spec)
+    try:
+        acc = tree[_spec_id]  # current accumulator
+    except KeyError:
+        acc = tree[_spec_id] = _spec_type()
+    if _spec_type is dict:
+        done = True
+        for keyspec, valspec in spec.items():
+            if tree.get(keyspec, None) is STOP:
+                continue
+            key = recurse(keyspec)
+            if key is SKIP:
+                done = False  # SKIP means we still want more vals
+                continue
+            if key is STOP:
+                tree[keyspec] = STOP
+                continue
+            if key not in acc:
+                # TODO: guard against key == id(spec)
+                tree[key] = {}
+            scope[ACC_TREE] = tree[key]
+            result = recurse(valspec)
+            if result is STOP:
+                tree[keyspec] = STOP
+                continue
+            done = False  # SKIP or returning a value means we still want more vals
+            if result is not SKIP:
+                acc[key] = result
+        if done:
+            return STOP
+        return acc
+    elif _spec_type is list:
+        for valspec in spec:
+            if type(valspec) is dict:
+                # doesn't make sense due to arity mismatch. did you mean [Auto({...})] ?
+                raise BadSpec('dicts within lists are not allowed while in Group mode: %r' % spec)
+            result = recurse(valspec)
+            if result is STOP:
+                return STOP
+            if result is not SKIP:
+                acc.append(result)
+        return acc
+    raise ValueError(f"{_spec_type} not a valid spec type for Group mode")  # pragma: no cover
 
 
 class First:
```
