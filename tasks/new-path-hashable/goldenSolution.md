# Golden solution -- new-path-hashable

**Task:** Make Path hashable, consistently with its equality
**Source:** net-new
**Target symbol:** `glom.core.Path`
**Detector:** `eq_without_hash`

## Why this is the correct fix

The reference solution adds `__hash__` to `Path` returning `hash(self.path_t.__ops__)`.

That expression is not arbitrary: it is the same expression the class's existing `__eq__` compares. Deriving the hash from whatever equality already uses is what guarantees the invariant `a == b implies hash(a) == hash(b)`; hashing any other attribute would produce a class that is hashable but broken as a dict key.

The verifier does not require this particular expression -- it tests the invariant, so any consistent hash passes.

## Verified behaviours

The verifier grades 6 case(s):

  - `tests_task/test_glom_core_path_hashable.py::test_instances_are_hashable`
  - `tests_task/test_glom_core_path_hashable.py::test_equal_instances_hash_equally`
  - `tests_task/test_glom_core_path_hashable.py::test_usable_as_dict_key`
  - `tests_task/test_glom_core_path_hashable.py::test_hash_is_stable_across_calls`
  - `tests_task/test_glom_core_path_hashable.py::test_set_deduplicates_equal_instances`
  - `tests_task/test_glom_core_path_hashable.py::test_unequal_instances_remain_distinct`

## Diff

```diff
--- a/glom/core.py
+++ b/glom/core.py
@@ -718,6 +718,9 @@
         elif type(other) is TType:
             return self.path_t.__ops__ == other.__ops__
         return False
+
+    def __hash__(self):
+        return hash(self.path_t.__ops__)
 
     def __ne__(self, other):
         return not self == other
```
