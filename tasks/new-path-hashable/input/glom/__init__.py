from glom._version import __version__
from glom.core import (
    MODE,
    ROOT,
    SKIP,
    STOP,
    UP,
    A,
    Auto,
    BadSpec,
    Call,
    Coalesce,
    CoalesceError,
    Fill,
    GlomError,
    Glommer,
    Inspect,
    Invoke,
    Path,
    PathAccessError,
    PathAssignError,
    Pipe,
    Ref,
    S,
    Spec,
    T,
    UnregisteredTarget,
    Val,
    Vars,
    glom,
    register,
    register_op,
)
from glom.matching import (
    And,
    Check,
    CheckError,
    M,
    Match,
    MatchError,
    Not,
    Optional,
    Or,
    Regex,
    Required,
    Switch,
    TypeMatchError,
)
from glom.mutation import Assign, Delete, PathDeleteError, assign, delete
from glom.reduction import Flatten, Fold, FoldError, Merge, Sum, flatten, merge

# there's no -ion word that really fits what "streaming" means.
# generation, production, iteration, all have more relevant meanings
# elsewhere. (maybe procrastination :P)
from glom.streaming import Iter
