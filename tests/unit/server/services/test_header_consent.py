"""What a connection-less row consents to, per vendor.

A row that authenticates by header has no consent record, and the platform reads
that as consent to nothing. That stays true for every vendor that publishes its
own endpoint. It is deliberately not true for a connector the operator deploys
beside the server, which has no consent screen to reach: the fixed answer there is
what makes the connector usable at all, and what these tests hold in place is that
no other vendor's answer moved.
"""

from __future__ import annotations

import pytest

from src.server.services.brokerage_capabilities import (
    denied_tools,
    group_keys_for,
    header_consent,
    order_tool,
)
from src.server.services.brokerages import BROKERAGES
from src.server.services.tool_binding import BindingInputs, resolve_plan

HOSTED = [b.name for b in BROKERAGES if b.operator_hosted]
OWN_ENDPOINT = [b.name for b in BROKERAGES if not b.operator_hosted]


@pytest.mark.parametrize("vendor", OWN_ENDPOINT)
def test_a_vendors_own_endpoint_still_consents_to_nothing(vendor):
    assert header_consent(vendor) == ()
    # And the consequence the rest of the platform relies on: its curation is denied.
    assert denied_tools(vendor, header_consent(vendor))


@pytest.mark.parametrize("vendor", HOSTED)
def test_an_operator_hosted_connector_consents_to_every_group_it_offers(vendor):
    assert header_consent(vendor) == group_keys_for(vendor) != ()
    assert denied_tools(vendor, header_consent(vendor)) == frozenset()


@pytest.mark.parametrize("vendor", [None, "", "nonesuch"])
def test_an_unknown_vendor_consents_to_nothing(vendor):
    assert header_consent(vendor) == ()


@pytest.mark.parametrize("vendor", HOSTED)
def test_its_order_tools_still_bind_direct_so_the_approval_gate_sees_them(vendor):
    plan = resolve_plan(
        vendor, header_consent(vendor), BindingInputs(relayable=True), candidates=()
    )
    order_tools = [
        t for t in plan.direct if order_tool(vendor, t) is not None
    ]
    assert order_tools, "an operator-hosted broker's orders must take the gated path"
    for tool in order_tools:
        assert plan.sandbox_excluded and tool in plan.sandbox_excluded
