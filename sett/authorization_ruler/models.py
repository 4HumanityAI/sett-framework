"""Same-object compatibility imports for existing authorization paths."""

from sett._governance.validation import (
    _validated_identifiers as _validated_identifiers,
)
from sett._governance.values import (
    ApprovalInput as ApprovalInput,
)
from sett._governance.values import (
    ApprovalRequirement as ApprovalRequirement,
)
from sett._governance.values import (
    AuthorizationConstraint as AuthorizationConstraint,
)
from sett._governance.values import (
    AuthorizationDecision as AuthorizationDecision,
)
from sett._governance.values import (
    AuthorizationOutcome as AuthorizationOutcome,
)
from sett._governance.values import (
    AuthorizationReceipt as AuthorizationReceipt,
)
from sett._governance.values import (
    AuthorizationRequest as AuthorizationRequest,
)
from sett._governance.values import (
    ExecutionMode as ExecutionMode,
)
from sett._governance.values import (
    FieldTrust as FieldTrust,
)
from sett._governance.values import (
    FieldValue as FieldValue,
)
from sett._governance.values import (
    ReceiptUse as ReceiptUse,
)
from sett._governance.values import (
    VerifiedApproval as VerifiedApproval,
)
from sett._governance.verdict import AuthorizationVerdict as AuthorizationVerdict
from sett.policy_ruler.models import (
    PolicyApplicability as PolicyApplicability,
)
from sett.policy_ruler.models import (
    PolicyApplicabilityState as PolicyApplicabilityState,
)
from sett.policy_ruler.models import (
    PolicyBinding as PolicyBinding,
)
from sett.policy_ruler.models import (
    PolicyDecision as PolicyDecision,
)
from sett.policy_ruler.models import (
    PolicyOrigin as PolicyOrigin,
)
from sett.policy_ruler.models import (
    PolicySelection as PolicySelection,
)
from sett.policy_ruler.models import (
    PolicySourceReference as PolicySourceReference,
)
from sett.policy_ruler.models import (
    _policy_identifier as _policy_identifier,
)
from sett.policy_ruler.models import (
    _policy_origin_key as _policy_origin_key,
)
from sett.policy_ruler.models import (
    _policy_origins as _policy_origins,
)
from sett.policy_ruler.models import (
    _policy_text as _policy_text,
)
from sett.policy_ruler.models import (
    _selection_facts as _selection_facts,
)
from sett.policy_ruler.models import (
    _selection_json_limit as _selection_json_limit,
)
from sett.policy_ruler.models import (
    _selection_sequence as _selection_sequence,
)
from sett.policy_ruler.models import (
    _selection_time as _selection_time,
)
