"""Known native restrictions remain visible without granting authority."""
from copy import deepcopy

from hermes_cli.kanban_wait_explanation import gate_explanation


def test_literal_native_clauses_and_nested_authority_survive_projection():
    gate = {'evaluator': 'attestation', 'reason': 'Native external restriction requires explicit qualified attestation',
            'current_unblock_action': 'Record authorised evidence for gate x',
            'parameters': {'native_condition': {'reason': 'No paid inference is authorised.',
                'resume_condition': 'Adrian grants the bounded window.',
                'selected_next_action': {'resume_condition': 'Exact runtime fit is independently evidenced.'}}}}
    before = deepcopy(gate)
    reason, action = gate_explanation(gate)
    assert reason == 'No paid inference is authorised.\nAdrian grants the bounded window.\nExact runtime fit is independently evidenced.'
    assert reason in action and action.endswith(gate['current_unblock_action'])
    assert gate == before


def test_opaque_and_protected_gates_keep_their_declared_explanation():
    for evaluator, native in [('attestation', {}), ('explicit_stop', {'reason': 'untrusted replacement'})]:
        gate = {'evaluator': evaluator, 'reason': 'Preserve original hold',
                'current_unblock_action': 'Use the protected native transition',
                'parameters': {'native_condition': native}}
        assert gate_explanation(gate) == (gate['reason'], gate['current_unblock_action'])
