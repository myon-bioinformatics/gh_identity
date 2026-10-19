"""Offline regression tests for variable-delete operation and write receipts."""
import contextlib
import io
import json
from unittest.mock import patch

import pytest
import gh_identity as ghi


def test_variable_delete_already_satisfied():
    with patch.object(ghi, 'request', side_effect=ghi.Error('not_found_or_inaccessible')):
        result = ghi.delete_variable('o/r', 'VAR')
    assert result['schema'] == 'gh-identity-variable-delete/1'
    assert result['status'] == 'already_satisfied'
    assert result['before'] is None


def test_variable_delete_plan_mode():
    var_data = {'name': 'VAR', 'value': 'VAL', 'updated_at': '2026-01-01T00:00:00Z'}
    with patch.object(ghi, 'request', return_value=var_data):
        result = ghi.delete_variable('o/r', 'VAR', write=False)
    assert result['schema'] == 'gh-identity-variable-delete/1'
    assert result['status'] == 'planned'
    assert result['before'] == 'VAR'
    assert result['mutation_status'] == 'not_attempted'


def test_variable_delete_write_verified():
    var_data = {'name': 'VAR', 'value': 'VAL', 'updated_at': '2026-01-01T00:00:00Z'}
    # 1st call: GET variable (exists)
    # 2nd call: DELETE variable (succeeds)
    # 3rd call: GET variable (returns 404 -> verified deleted)
    responses = [var_data, None, ghi.Error('not_found_or_inaccessible')]
    with patch.object(ghi, 'request', side_effect=responses) as req:
        result = ghi.delete_variable('o/r', 'VAR', write=True)
    assert result['status'] == 'verified'
    assert result['mutation_status'] == 'succeeded'
    assert result['verification_status'] == 'verified'
    assert result['verified'] is True
    assert req.call_count == 3
    assert req.call_args_list[1].args[0] == 'DELETE'
    assert 'repos/o/r/actions/variables/VAR' in req.call_args_list[1].args[1]


def test_variable_delete_write_verification_mismatch():
    var_data = {'name': 'VAR', 'value': 'VAL', 'updated_at': '2026-01-01T00:00:00Z'}
    # DELETE succeeds, but subsequent GET still finds the variable
    responses = [var_data, None, var_data]
    with patch.object(ghi, 'request', side_effect=responses):
        result = ghi.delete_variable('o/r', 'VAR', write=True)
    assert result['status'] == 'verification_mismatch'
    assert result['mutation_status'] == 'succeeded'
    assert result['verification_status'] == 'mismatch'
    assert result['verified'] is False


def test_variable_delete_write_mutation_uncertain():
    var_data = {'name': 'VAR', 'value': 'VAL', 'updated_at': '2026-01-01T00:00:00Z'}
    # 1st call GET succeeds; DELETE raises uncertain Error
    responses = [var_data, ghi.Error('operation_timeout', uncertain=True)]
    with patch.object(ghi, 'request', side_effect=responses):
        result = ghi.delete_variable('o/r', 'VAR', write=True)
    assert result['status'] == 'mutation_uncertain'
    assert result['mutation_status'] == 'uncertain'


def test_variable_delete_cli_exit_codes():
    # Planned -> exit 0
    var_data = {'name': 'VAR', 'value': 'VAL', 'updated_at': '2026-01-01T00:00:00Z'}
    with patch.object(ghi, 'request', return_value=var_data), contextlib.redirect_stdout(io.StringIO()):
        assert ghi.main(['variable-delete', 'o/r', 'VAR']) == 0

    # Already satisfied -> exit 0
    with patch.object(ghi, 'request', side_effect=ghi.Error('not_found_or_inaccessible')), contextlib.redirect_stdout(io.StringIO()):
        assert ghi.main(['variable-delete', 'o/r', 'VAR']) == 0

    # Verified -> exit 0
    responses = [var_data, None, ghi.Error('not_found_or_inaccessible')]
    with patch.object(ghi, 'request', side_effect=responses), contextlib.redirect_stdout(io.StringIO()):
        assert ghi.main(['variable-delete', 'o/r', 'VAR', '--write']) == 0

    # Verification mismatch -> exit 1
    responses = [var_data, None, var_data]
    with patch.object(ghi, 'request', side_effect=responses), contextlib.redirect_stdout(io.StringIO()):
        assert ghi.main(['variable-delete', 'o/r', 'VAR', '--write']) == 1
