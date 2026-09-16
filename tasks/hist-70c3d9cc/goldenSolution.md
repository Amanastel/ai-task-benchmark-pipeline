# Golden solution -- hist-70c3d9cc

**Task:** glom.control_flow: Cleaner control flow in Switch.glomit
**Source:** history
**Commit:** `70c3d9cc3f09bd0bd72ab96c5bbc78839b7fa6ea` (parent `b4215902bf02`)
**Upstream:** https://github.com/mahmoud/glom/commit/70c3d9cc3f09bd0bd72ab96c5bbc78839b7fa6ea

## Why this is the correct fix

The reference solution is the upstream change 70c3d9cc3f09 ("more tests, cleaner control flow in Switch.glomit"), authored on 2020-07-05T10:55:02-07:00.

It is correct because the project's own maintainers shipped it as the fix for this behaviour, and because the 1 verifier case(s) that fail against the parent commit all pass against it. Those cases were selected mechanically: the post-commit test files were run against both trees and only the tests whose outcome flipped from failing to passing were kept, so the verifier measures this change and not the pre-existing behaviour around it.

Files changed: glom/control_flow.py.

## Verified behaviours

The verifier grades 1 case(s):

  - `glom/test/test_control_flow.py::test_switch`

## Diff

```diff
diff --git a/glom/control_flow.py b/glom/control_flow.py
index 7e169db..46680b6 100644
--- a/glom/control_flow.py
+++ b/glom/control_flow.py
@@ -3,6 +3,7 @@ Control flow primitives of glom.
 """
 
 from glom import glom, GlomError
+from .core import bbrepr
 
 
 _MISSING = object()
@@ -28,25 +29,25 @@ class Switch(object):
             cases = list(cases.items())
         if type(cases) is not list:
             raise TypeError(
-                "cases must be {keyspec: valspec} or "
+                "cases must be {{keyspec: valspec}} or "
                 "[(keyspec, valspec)] not {}".format(type(cases)))
         self.cases = cases
         # glom.match(cases, Or([(object, object)], dict))
         # start dogfooding ^
         self.default = default
+        if not cases and self.default is _MISSING:
+            raise ValueError('Switch() without cases or default will always error')
 
     def glomit(self, target, scope):
         for keyspec, valspec in self.cases:
             try:
                 scope[glom](target, keyspec, scope)
-                break
-            except GlomError:
-                pass
-        else:
-            if self.default is not _MISSING:
-                return default
-            raise GlomError("no matches for target in Switch")
-        return scope[glom](target, valspec, scope)
+            except GlomError as ge:
+                continue
+            return scope[glom](target, valspec, scope)
+        if self.default is not _MISSING:
+            return self.default
+        raise GlomError("no matches for target in Switch")
 
     def __repr__(self):
-        return "Switch(" + repr(self.cases) + ")"
+        return "Switch(" + bbrepr(self.cases) + ")"
```
