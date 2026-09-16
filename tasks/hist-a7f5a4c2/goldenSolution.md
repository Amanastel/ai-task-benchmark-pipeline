# Golden solution -- hist-a7f5a4c2

**Task:** glom.mutation: Assign.__repr__
**Source:** history
**Commit:** `a7f5a4c2fefe2aee31a8ce9c893118f7b1d5f7a2` (parent `ec360f496f2c`)
**Upstream:** https://github.com/mahmoud/glom/commit/a7f5a4c2fefe2aee31a8ce9c893118f7b1d5f7a2

## Why this is the correct fix

The reference solution is the upstream change a7f5a4c2fefe ("Assign.__repr__"), authored on 2019-10-28T23:07:16-07:00.

It is correct because the project's own maintainers shipped it as the fix for this behaviour, and because the 2 verifier case(s) that fail against the parent commit all pass against it. Those cases were selected mechanically: the post-commit test files were run against both trees and only the tests whose outcome flipped from failing to passing were kept, so the verifier measures this change and not the pre-existing behaviour around it.

Files changed: glom/mutation.py.

## Verified behaviours

The verifier grades 2 case(s):

  - `glom/test/test_basic.py::test_api_repr`
  - `glom/test/test_mutation.py::test_assign`

## Diff

```diff
diff --git a/glom/mutation.py b/glom/mutation.py
index a70933c..aa036ba 100644
--- a/glom/mutation.py
+++ b/glom/mutation.py
@@ -178,6 +178,12 @@ class Assign(object):
 
         return target
 
+    def __repr__(self):
+        cn = self.__class__.__name__
+        if self.missing is None:
+            return '%s(%r, %r)' % (cn, self._orig_path, self.val)
+        return '%s(%r, %r, missing=%r)' % (cn, self._orig_path, self.val, self.missing)
+
 
 def assign(obj, path, val, missing=None):
     """The ``assign()`` function provides convenient "deep set"
```
