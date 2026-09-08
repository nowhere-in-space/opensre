"""Nothing in this build contacts a vendor service on its own.

Four things used to: product analytics, error reporting, the account check the
interactive shell runs at startup, and the release check behind ``update`` and
``doctor``. None is asked for by the operator, and all four fail badly inside a
customer's cloud, where those hosts are unreachable and "nothing leaves this
machine" is a condition of being allowed to run at all.

Each site is asserted separately rather than through the constant alone: a site
that stops consulting it would leave the constant looking honest while the
traffic resumed.
"""

from __future__ import annotations

import pytest

from config.constants.vendor_services import VENDOR_SERVICES_ENABLED


def _not_a_test_run() -> bool:
    """Stand in for the pytest detection, which would pass the gate on its own."""
    return False


def test_this_build_declares_itself_offline() -> None:
    assert VENDOR_SERVICES_ENABLED is False


def test_analytics_stays_opted_out_with_no_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env opt-outs are a courtesy; the guarantee cannot rest on them.

    The prompt-log sink rides on this same provider, so prompts and responses -
    host names, cluster ids, log excerpts - travel with it.
    """
    from infrastructure.analytics.provider import _is_opted_out

    for name in ("OPENSRE_NO_TELEMETRY", "OPENSRE_ANALYTICS_DISABLED", "DO_NOT_TRACK"):
        monkeypatch.delenv(name, raising=False)

    assert _is_opted_out() is True


def test_error_reporting_has_no_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """A report carries a stack and its locals - the last thing that should leave."""
    from infrastructure.observability.errors.sentry import sentry_transport_enabled

    for name in ("OPENSRE_NO_TELEMETRY", "OPENSRE_SENTRY_DISABLED", "DO_NOT_TRACK"):
        monkeypatch.delenv(name, raising=False)

    assert sentry_transport_enabled() is False


def test_the_shell_opens_without_an_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate counts an unreachable app as "not signed in", so it withholds the shell."""
    from rich.console import Console

    from surfaces.interactive_shell.runtime.startup import account_gate

    monkeypatch.setattr(account_gate, "is_test_run", _not_a_test_run)

    def _fail_if_called() -> bool:
        raise AssertionError("the account state was consulted")

    monkeypatch.setattr(account_gate, "account_is_signed_in", _fail_if_called)

    assert account_gate.pass_sign_in_gate(Console()) is True


def test_update_says_so_instead_of_reaching_the_release_host(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Not an error: a network failure would send the operator fixing the wrong thing."""
    from surfaces.cli.lifecycle.update import run_update

    assert run_update(check_only=True) == 0
    assert "does not check for releases" in capsys.readouterr().out


def test_doctor_reports_the_version_check_as_skipped() -> None:
    from surfaces.shared.doctor_checks import _check_version_freshness

    passed, detail = _check_version_freshness()

    assert passed is True
    assert "does not check for releases" in detail
