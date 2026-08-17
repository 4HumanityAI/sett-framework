"""
SETT Framework: Google Cloud credential loading, shared across adapters
==============================
F18 fix (defensive audit #2).

``GoogleTTSAdapter``, ``GoogleSTTAdapter`` and ``GoogleSentimentAdapter``
each accepted an optional ``credentials_path`` and, when given, used to
do ``os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = credentials_path``
before constructing their client. That mutates process-global state: two
instances of these adapters configured with two different service
account files in the same process would interfere with each other -
whichever constructed last would silently win for both, and every other
Google client anywhere else in that process (including ones this
framework does not know about) would pick up the mutation too.

Google's client libraries accept an explicit ``credentials=`` object on
construction, which is the documented way to scope credentials to one
client instance without touching the environment at all. This module
builds that object from a service account file.

Omitting ``credentials_path`` leaves the client's own Application
Default Credentials resolution untouched, in full: the environment
variable, a ``gcloud`` login, GCE/GKE metadata and Workload Identity all
work, because this module never second-guesses which of them applies.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sett.exceptions import SETTError, SETTServiceAdapterError


def google_credentials_from_path(credentials_path: str | None) -> Any | None:
    """Build an explicit ``Credentials`` object from a service account file.

    Args:
        credentials_path: Path to a Google Cloud service account JSON
            key, or ``None``.

    Returns:
        A ``google.auth.credentials.Credentials`` instance to pass as
        ``credentials=`` to a client constructor, or ``None`` when no
        path was given, in which case the caller passes ``None``
        through unchanged and the client falls back to Application
        Default Credentials exactly as before this fix.

    Raises:
        SETTServiceAdapterError: If ``google-auth`` is unavailable (it is
            a transitive dependency of every ``google-cloud-*`` package
            these adapters already require) or the file cannot be
            loaded.
    """
    if not credentials_path:
        return None
    try:
        from google.oauth2 import service_account
    except ImportError as error:
        raise SETTServiceAdapterError(
            "The 'google-auth' package is required to load credentials "
            "from a file path. It is a transitive dependency of every "
            "google-cloud-* package this framework's Google adapters "
            "already require; reinstall the adapter's extra if it is "
            "somehow missing."
        ) from error
    try:
        return service_account.Credentials.from_service_account_file(
            credentials_path
        )
    except Exception as error:
        raise SETTServiceAdapterError(
            f"Could not load Google Cloud credentials from "
            f"{credentials_path!r}: {error}"
        ) from error


def build_google_client(
    factory: Callable[..., Any],
    *,
    credentials: Any | None,
    adapter_name: str,
) -> Any:
    """Construct a Google client, failing closed with a framework error.

    Replaces an earlier guard that asked whether
    ``GOOGLE_APPLICATION_CREDENTIALS`` was set before allowing the client
    to be built. That guard was wrong in a way worth recording: Application
    Default Credentials resolve from five sources: that environment
    variable, a ``gcloud`` user login, GCE/GKE metadata, Workload Identity,
    and the well-known config file. Checking one of the five refused
    four valid configurations. On GKE it made the framework unusable
    without a service account key file, which is the credential type
    Google recommends avoiding there, so the check pushed applications
    toward the weaker option in the name of safety.

    Asking the client library is the only correct way to answer "are there
    credentials", because it is the thing that resolves them. It already
    fails closed with ``DefaultCredentialsError`` when there are none, and
    its message names every source it looked at, which is more useful than
    anything this module could reconstruct. All that is added here is the
    framework's own exception type on the way out.

    Everything this raises is a ``SETTServiceAdapterError``: a missing
    credential gets the message above, and any other construction failure
    gets a generic one with the original kept as ``__cause__``. SETT's own
    exceptions, such as a cancellation or a deadline reaching this from inside
    ``factory``, pass through with their type intact.
    """
    try:
        return factory(credentials=credentials)
    except SETTError:
        raise
    except Exception as error:
        if type(error).__name__ == "DefaultCredentialsError":
            raise SETTServiceAdapterError(
                f"Google Cloud credentials not found for {adapter_name}. "
                "Pass credentials_path= with a service account JSON key, or "
                "configure Application Default Credentials (gcloud auth "
                "application-default login, GOOGLE_APPLICATION_CREDENTIALS, "
                "or the workload identity of the machine this runs on). The "
                f"client library reported: {error}"
            ) from error
        # Missing credentials is the failure worth its own message; every
        # other way a client constructor can fail still leaves as a
        # framework error rather than as whatever type the Google library
        # happened to choose. An earlier version re-raised those
        # untranslated, which meant "build a Google client" had one
        # documented failure type and an open-ended set of undocumented
        # ones, following the same enumerate-the-known-failures shape that
        # ``OllamaAdapter._post`` already had to abandon.
        raise SETTServiceAdapterError(
            f"Could not construct the Google client for {adapter_name}: "
            f"{type(error).__name__}: {error}"
        ) from error
