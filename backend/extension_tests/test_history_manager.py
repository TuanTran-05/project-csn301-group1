"""Run with unittest; no Flask or database; uses the existing cryptography dependency."""
import json
import unittest
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from network_copilot.history_extension import HistoryManager
from network_copilot.history_extension.protection import decode_backup
from network_copilot.history_extension.history_manager import _diff


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.manager = HistoryManager()

    def push(self, value, topology="default"):
        return self.manager.push_state(value, "operator", "CONFIG_INT", "Edit draft", topology)

    def test_baseline_and_boundaries(self):
        self.assertIsNone(self.manager.undo())
        self.assertIsNone(self.manager.redo())
        self.push({"vlan": 10})
        self.assertFalse(self.manager.can_undo())
        self.push({"vlan": 20})
        self.assertEqual(self.manager.undo(actor="alice"), {"vlan": 10})
        self.assertFalse(self.manager.can_undo())
        self.assertTrue(self.manager.can_redo())
        self.assertEqual(self.manager.redo(actor="bob"), {"vlan": 20})
        self.assertFalse(self.manager.can_redo())

    def test_branch_and_audit_survive_export_restore(self):
        for n in range(3):
            self.push({"n": n})
        self.manager.undo(actor="alice")
        backup = self.manager.export_backup_file()
        restored = HistoryManager()
        self.assertEqual(restored.restore_from_backup(backup), {"n": 1})
        self.assertEqual(restored.redo(), {"n": 2})
        self.push({"n": 99})
        self.assertFalse(self.manager.can_redo())
        trail = decode_backup(json.loads(self.manager.export_backup_file()), self.manager._cipher)["audit_trail"]
        self.assertEqual([e["snapshot"]["n"] for e in trail], [0, 1, 2, 1, 99])
        self.assertEqual(trail[3]["actor"], "alice")

    def test_diff_nested_types_and_pointer_escaping(self):
        self.push({"a/b": {"~x": True}, "deleted": 1, "array": [1]})
        self.push({"a/b": {"~x": 1}, "added": None, "array": [1, 2]})
        changes = _diff(self.manager.active_snapshots()[0], self.manager.current_snapshot())
        self.assertEqual({c["path"]: c["op"] for c in changes}, {
            "/a~1b/~0x": "replace", "/added": "add", "/array": "replace", "/deleted": "remove"})

    def test_mutable_inputs_and_outputs_are_isolated(self):
        original = {"nodes": [{"id": 1}]}
        self.push(original)
        original["nodes"][0]["id"] = 9
        result = self.manager.current_snapshot()
        result["nodes"].clear()
        self.assertEqual(self.manager.current_snapshot(), {"nodes": [{"id": 1}]})

    def test_boolean_and_number_inside_list_are_different(self):
        self.push({"values": [True]})
        self.push({"values": [1]})
        self.assertEqual(self.manager.get_diff()[0]["path"], "/values")

    def test_invalid_restore_is_atomic(self):
        self.push({"keep": True})
        original = self.manager.export_backup_file()
        # Exercise legacy stack validation itself; encrypted envelope tampering
        # is checked separately by the hardening tests.
        public_before = json.loads(original)
        valid = decode_backup(public_before, self.manager._cipher)
        invalid = ["{", {}, {**valid, "backup_version": "2"},
                   {**valid, "history_state": {"undo_stack": [99], "redo_stack": []}},
                   {**valid, "history_state": {"undo_stack": [0], "redo_stack": [0]}}]
        for data in invalid:
            with self.assertRaises(ValueError):
                self.manager.restore_from_backup(data)
            self.assertEqual(self.manager.status(), {"snapshot": {"keep": True}, "can_undo": False, "can_redo": False, "diff": []})
            exported = json.loads(self.manager.export_backup_file())
            self.assertEqual(exported["history_state"], valid["history_state"])
            self.assertEqual(exported["audit_trail"], public_before["audit_trail"])

    def test_topology_isolation_and_restore_keeps_receiving_audit(self):
        self.push({"n": 1}, "a")
        self.push({"n": 2}, "a")
        self.push({"n": 10}, "b")
        self.manager.undo("a")
        self.assertEqual(self.manager.current_snapshot("b"), {"n": 10})
        self.manager.restore_from_backup(self.manager.export_backup_file("a"), "b", actor="admin")
        trail = decode_backup(json.loads(self.manager.export_backup_file("b")), self.manager._cipher)["audit_trail"]
        self.assertEqual(trail[0]["snapshot"], {"n": 10})
        self.assertEqual(trail[-1]["action_type"], "RESTORE")
        self.assertTrue(self.manager.can_redo("b"))

    def test_minimal_backup_and_empty_roundtrip(self):
        for n in range(2):
            self.push({"n": n})
        backup = decode_backup(json.loads(self.manager.export_backup_file()), self.manager._cipher)
        backup.pop("history_state")
        restored = HistoryManager()
        restored.restore_from_backup(backup)
        self.assertEqual(restored.undo(), {"n": 0})
        empty = HistoryManager()
        empty.restore_from_backup(HistoryManager().export_backup_file())
        empty.restore_from_backup(empty.export_backup_file())
        self.assertIsNone(empty.current_snapshot())

    def test_non_json_values_rejected_and_concurrent_pushes(self):
        for value in ({1: "bad"}, {"n": float("nan")}, {"n": (1, 2)}):
            with self.assertRaises(ValueError):
                self.push(value)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda n: self.push({"n": n}), range(30)))
        trail = json.loads(self.manager.export_backup_file())["audit_trail"]
        self.assertEqual([e["step"] for e in trail], list(range(1, 31)))

    def test_noop_undo_redo_do_not_create_audit_events(self):
        self.manager.undo()
        self.manager.redo()
        self.assertEqual(json.loads(self.manager.export_backup_file())["audit_trail"], [])
        self.push({"interfaces": {}})
        self.manager.undo()
        self.manager.redo()
        self.assertEqual(len(json.loads(self.manager.export_backup_file())["audit_trail"]), 1)

    def test_multiple_undo_redo_preserve_order_actor_and_audit(self):
        for n in range(3):
            self.push({"description": str(n)})
        self.assertEqual(self.manager.undo(actor="alice"), {"description": "1"})
        self.assertEqual(self.manager.undo(actor="bob"), {"description": "0"})
        self.assertEqual(self.manager.redo(actor="alice"), {"description": "1"})
        self.assertEqual(self.manager.redo(actor="bob"), {"description": "2"})
        data = json.loads(self.manager.export_backup_file())
        self.assertEqual(data["timezone"], "UTC+07:00")
        self.assertEqual([e["action_type"] for e in data["audit_trail"]][-4:], ["UNDO", "UNDO", "REDO", "REDO"])
        private = decode_backup(data, self.manager._cipher)
        self.assertEqual([e["actor"] for e in private["audit_trail"]][-4:], ["alice", "bob", "alice", "bob"])
        self.assertEqual([e["actor"] for e in data["audit_trail"]][-4:], ["actor-2", "actor-3", "actor-2", "actor-3"])
        for event in data["audit_trail"]:
            self.assertEqual(datetime.strptime(event["timestamp"], "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S"), event["timestamp"])
            self.assertIsInstance(event["diff_from_previous"], str)

    def test_restore_recomputes_untrusted_diff_and_appends_operator_event(self):
        self.push({"description": "before"})
        self.push({"description": "after"})
        data = decode_backup(json.loads(self.manager.export_backup_file()), self.manager._cipher)
        data["audit_trail"][-1]["diff_from_previous"] = "forged summary"
        restored = HistoryManager()
        restored.restore_from_backup(data, actor="importing-admin")
        audit = decode_backup(json.loads(restored.export_backup_file()), restored._cipher)["audit_trail"]
        self.assertEqual(audit[1]["diff_from_previous"], "replace /description")
        self.assertEqual(audit[-1]["actor"], "importing-admin")
        self.assertEqual(audit[-1]["action_type"], "RESTORE")
        data["audit_trail"][1]["snapshot"]["description"] = "mutated caller input"
        self.assertEqual(restored.current_snapshot(), {"description": "after"})


if __name__ == "__main__":
    unittest.main()
