"""
Tests für E8/W8.2/T8.2.3: Terminale Job-Status dürfen nicht rückwärts laufen.
"""

from unittest.mock import patch

from conftest import load_server_module


_server = load_server_module(use_real_pil=False)


class _FakeCursor:
    def __init__(self, state):
        self.state = state
        self._row = None

    def execute(self, query, params=None):
        normalized = " ".join(query.split())
        if normalized.startswith("INSERT INTO job"):
            job_id = params[0]
            self.state[job_id] = {
                "id": job_id,
                "status": "queued",
                "progress_json": None,
                "result_json": None,
            }
            return

        if "SET result_json=%s, status='completed'" in normalized:
            result_json, job_id = params
            self.state[job_id]["result_json"] = result_json
            self.state[job_id]["status"] = "completed"
            return

        if "WHERE id=%s AND status NOT IN ('completed', 'failed')" in normalized:
            status, progress_json, job_id = params
            if self.state[job_id]["status"] not in {"completed", "failed"}:
                self.state[job_id]["status"] = status
                self.state[job_id]["progress_json"] = progress_json
            return

        if normalized.startswith("SELECT * FROM job WHERE id=%s"):
            job_id = params[0]
            self._row = self.state.get(job_id)
            return

        raise AssertionError(f"Unsupported query in fake DB: {query}")

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, state):
        self.state = state

    def cursor(self):
        return _FakeCursor(self.state)

    def commit(self):
        pass


class TestJobStatusMonotonic:
    def test_late_progress_update_does_not_override_completed_status(self):
        state = {}
        conn = _FakeConn(state)
        module_globals = _server.job_create.__globals__

        with patch.object(_server, "get_db_connection", return_value=conn), \
             patch.dict(module_globals, {"_return_conn": lambda _conn: None}):
            _server.job_create("job-1")
            _server.job_set_result("job-1", '{"success": true}')
            _server.job_update("job-1", "processing", '{"percent": 99}')
            job = _server.job_get("job-1")

        assert job["status"] == "completed"
        assert job["result_json"] == '{"success": true}'

    def test_late_progress_update_does_not_override_failed_status(self):
        state = {}
        conn = _FakeConn(state)
        module_globals = _server.job_create.__globals__

        with patch.object(_server, "get_db_connection", return_value=conn), \
             patch.dict(module_globals, {"_return_conn": lambda _conn: None}):
            _server.job_create("job-2")
            state["job-2"]["status"] = "failed"
            state["job-2"]["progress_json"] = '{"error": "boom"}'
            _server.job_update("job-2", "processing", '{"percent": 50}')
            job = _server.job_get("job-2")

        assert job["status"] == "failed"
        assert job["progress_json"] == '{"error": "boom"}'
