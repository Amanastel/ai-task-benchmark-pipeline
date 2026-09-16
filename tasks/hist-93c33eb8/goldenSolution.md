# Golden solution -- hist-93c33eb8

**Task:** glom.flat: Sum() pretty much ready to go
**Source:** history
**Commit:** `93c33eb8db268e4cd2a355664c2c52de427ed517` (parent `83020ca24324`)
**Upstream:** https://github.com/mahmoud/glom/commit/93c33eb8db268e4cd2a355664c2c52de427ed517

## Why this is the correct fix

The reference solution is the upstream change 93c33eb8db26 ("Sum() pretty much ready to go"), authored on 2019-01-16T19:02:38-08:00.

It is correct because the project's own maintainers shipped it as the fix for this behaviour, and because the 2 verifier case(s) that fail against the parent commit all pass against it. Those cases were selected mechanically: the post-commit test files were run against both trees and only the tests whose outcome flipped from failing to passing were kept, so the verifier measures this change and not the pre-existing behaviour around it.

Files changed: glom/flat.py.

## Verified behaviours

The verifier grades 2 case(s):

  - `glom/test/test_flat.py::test_sum_integers`
  - `glom/test/test_flat.py::test_sum_seqs`

## Diff

```diff
diff --git a/glom/flat.py b/glom/flat.py
index 2f48235..684f1bb 100644
--- a/glom/flat.py
+++ b/glom/flat.py
@@ -1,31 +1,33 @@
 
 from boltons.typeutils import make_sentinel
 
-from .core import TargetRegistry, Path
+from .core import TargetRegistry, Path, T, glom
 
 _MISSING = make_sentinel('_MISSING')
 
+# TODO: Sum, Flatten, and Reduce
+
 
 class Sum(object):
-    def __init__(self, default=None):
-        self.default = default
+    def __init__(self, subspec=T, start=0):
+        self.subspec = subspec
+        self.start = start
 
     def glomit(self, target, scope):
-        ret = _MISSING
+        ret = self.start
+
+        if self.subspec is not T:
+            target = scope[glom](target, self.subspec, scope)
 
         iterate = scope[TargetRegistry].get_handler('iterate', target, path=scope[Path])
 
         try:
             iterator = iterate(target)
         except Exception as e:
+            # TODO: should this be a GlomError of some form?
             raise TypeError('failed to iterate on instance of type %r at %r (got %r)'
                             % (target.__class__.__name__, Path(*scope[Path]), e))
 
-        for val in iterator:
-            if ret is _MISSING:
-                ret = type(val)()
-                continue
-            ret += val
-        if ret is _MISSING:
-            return self.default
+        for v in iterator:
+            ret += v
         return ret
```
