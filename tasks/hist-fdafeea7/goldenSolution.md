# Golden solution -- hist-fdafeea7

**Task:** glom.core: Fix a Path repr issue, add a bit more coverage
**Source:** history
**Commit:** `fdafeea746ba9f6a5e51a4801a4d3f5f85718fd8` (parent `b454244f7070`)
**Upstream:** https://github.com/mahmoud/glom/commit/fdafeea746ba9f6a5e51a4801a4d3f5f85718fd8

## Why this is the correct fix

The reference solution is the upstream change fdafeea746ba ("fix a Path repr issue, add a bit more coverage"), authored on 2018-07-01T22:39:27-07:00.

It is correct because the project's own maintainers shipped it as the fix for this behaviour, and because the 2 verifier case(s) that fail against the parent commit all pass against it. Those cases were selected mechanically: the post-commit test files were run against both trees and only the tests whose outcome flipped from failing to passing were kept, so the verifier measures this change and not the pre-existing behaviour around it.

Files changed: glom/core.py.

## Verified behaviours

The verifier grades 2 case(s):

  - `glom/test/test_target_types.py::test_default_scope_register`
  - `glom/test/test_target_types.py::test_faulty_iterate`

## Diff

```diff
diff --git a/glom/core.py b/glom/core.py
index 98ea760..09edcc0 100644
--- a/glom/core.py
+++ b/glom/core.py
@@ -292,7 +292,7 @@ class Path(object):
 
     def __repr__(self):
         # TODO: FIXME to not assume all parts are 'P'
-        path_parts = _T_PATHS[self.path_t][1::2]
+        path_parts = _T_PATHS[self.path_t][2::2]
         cn = self.__class__.__name__
         return '%s(%s)' % (cn, ', '.join([repr(p) for p in path_parts]))
 
@@ -828,9 +828,12 @@ def _handle_list(spec, target, scope):
         raise UnregisteredTarget('iterate', type(target), scope[_TargetRegistry]._type_map, path=scope[Path])
     try:
         iterator = handler.iterate(target)
-    except TypeError as te:
+    except Exception as e:
+        te = TypeError('failed to iterate on instance of type %r at %r (got %r)'
+                        % (target.__class__.__name__, Path(*scope[Path]), e))
+        print(te)
         raise TypeError('failed to iterate on instance of type %r at %r (got %r)'
-                        % (target.__class__.__name__, Path(*path), te))
+                        % (target.__class__.__name__, Path(*scope[Path]), e))
     ret = []
     for i, t in enumerate(iterator):
         val = scope[glom](t, subspec, scope.new_child({Path: scope[Path] + [i]}))
@@ -870,11 +873,11 @@ class _SpecRegistry(object):
             'no handler for specs of type {}; expected one of '
             '{}'.format(type(spec), ','.join(
                 [e[1] for e in self.specs if e[1] is not callable] + ['callable'])))
-        '''  # TODO: don't lose anything from older error message
-            raise TypeError('expected spec to be dict, list, tuple,'
-                            ' callable, string, or other specifier type,'
-                            ' not: %r'% spec)
-        '''
+        # TODO: don't lose anything from older error message
+        # raise TypeError('expected spec to be dict, list, tuple,'
+        #                 ' callable, string, or other specifier type,'
+        #                 ' not: %r'% spec)
+
 
     def register(self, spec_type, spec_handler):
         '''
@@ -1095,7 +1098,7 @@ def register(target_type, get=None, iterate=None, exact=False):
        methods instead.
 
     """
-    _DEFAULT_SPEC_REGISTRY[_TargetRegistry].register(target_type, get, iterate, exact)
+    _DEFAULT_SCOPE[_TargetRegistry].register(target_type, get, iterate, exact)
     return
```
