# Golden solution -- hist-0da761ff

**Task:** glom.reduction: Change strategy on Merge() such that the initial value will not be…
**Source:** history
**Commit:** `0da761ffea3e9d85d65b4dcd4e4f2e40ecea947b` (parent `08c801a37f7c`)
**Upstream:** https://github.com/mahmoud/glom/commit/0da761ffea3e9d85d65b4dcd4e4f2e40ecea947b

## Why this is the correct fix

The reference solution is the upstream change 0da761ffea3e ("change strategy on Merge() such that the initial value will not be operated on/updated to support the case of a lambda init"), authored on 2019-02-17T19:14:29-08:00.

It is correct because the project's own maintainers shipped it as the fix for this behaviour, and because the 2 verifier case(s) that fail against the parent commit all pass against it. Those cases were selected mechanically: the post-commit test files were run against both trees and only the tests whose outcome flipped from failing to passing were kept, so the verifier measures this change and not the pre-existing behaviour around it.

Files changed: glom/reduction.py.

## Verified behaviours

The verifier grades 2 case(s):

  - `glom/test/test_reduction.py::test_merge`
  - `glom/test/test_reduction.py::test_merge_omd`

## Diff

```diff
diff --git a/glom/reduction.py b/glom/reduction.py
index 3c2023b..6f089a9 100644
--- a/glom/reduction.py
+++ b/glom/reduction.py
@@ -9,6 +9,12 @@ from .core import TargetRegistry, Path, T, glom, GlomError, UnregisteredTarget
 _MISSING = make_sentinel('_MISSING')
 
 
+try:
+    basestring
+except NameError:
+    basestring = str
+
+
 class FoldError(GlomError):
     """Error raised when Fold() is called on non-iterable
     targets, and possibly other uses in the future."""
@@ -154,14 +160,13 @@ class Flatten(Fold):
 class Merge(Fold):
     def __init__(self, subspec=T, init=dict, op=None):
         if op is None:
-            try:
-                op = init.update
-                op(init(), init())  # take it out for a spin
-            except AttributeError:
-                raise ValueError('expected "op" arg or an init type with update method,'
-                                 ' not %r and %r' % (op, init))
-        elif not callable(op):
-            raise TypeError('expected "op" to be callable, not %r' % op)
+            op = 'update'
+        if isinstance(op, basestring):
+            test_init = init()
+            op = getattr(type(test_init), op, None)
+        if not callable(op):
+            raise ValueError('expected callable "op" arg or an "init" with an .update()'
+                             ' method not %r and %r' % (op, init))
         super(Merge, self).__init__(subspec=subspec, init=init, op=op)
 
     def _fold(self, iterator):
```
