import hashlib
import hmac
import json
import socket
import threading
import unittest

from scada_uploader.signing import (
    HEADER_DEVICE,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    assemble_body,
    auth_headers,
    render_payload,
    sign_body,
    signing_message,
    unix_timestamp,
)
from scada_uploader.transport import interpret_response, post_telemetry

# Published example key from the integration guide, section 8. Not a plant key.
GUIDE_SECRET = "scada_sec_key_2026_test_12345"
GUIDE_TIMESTAMP = "1780656091"
GUIDE_BODY = (
    '{"readingAt":"2026-07-03T11:00:00+05:30","moisture_mg_m3":3.2,'
    '"methane_mole_percent":96.8,"h2s_mg_m3":1.9,'
    '"totalSulphurIncludingH2s_mg_m3":7.4,"co2N2O2_mole_percent":3.6,'
    '"co2_mole_percent":2.8,"oxygen_mole_percent":0.3,'
    '"fresh_slurry_flow_scm_hr":25.4,"raw_biogas_flow_scm_hr":120.5,'
    '"purified_biogas_flow_scm_hr":110.2,"dispatch_flow_kg_hr":450.75,'
    '"slurry_FlowTotalizer_scm":12548750.75,"raw_FlowTotalizer_scm":11548750.11,'
    '"purified_FlowTotalizer_scm":15548750.09,"dispatch_flowTotalizer_kg":15548750.03}'
)
GUIDE_DIGEST = "d2b6f82d0b274df3cc578bd287f76d50baccec1b92a0ddccc23973126995b79e"

GUIDE_NUMBERS = {
    "moisture_mg_m3": "3.2",
    "methane_mole_percent": "96.8",
    "h2s_mg_m3": "1.9",
    "totalSulphurIncludingH2s_mg_m3": "7.4",
    "co2N2O2_mole_percent": "3.6",
    "co2_mole_percent": "2.8",
    "oxygen_mole_percent": "0.3",
    "fresh_slurry_flow_scm_hr": "25.4",
    "raw_biogas_flow_scm_hr": "120.5",
    "purified_biogas_flow_scm_hr": "110.2",
    "dispatch_flow_kg_hr": "450.75",
    "slurry_FlowTotalizer_scm": "12548750.75",
    "raw_FlowTotalizer_scm": "11548750.11",
    "purified_FlowTotalizer_scm": "15548750.09",
    "dispatch_flowTotalizer_kg": "15548750.03",
}


class SigningTests(unittest.TestCase):
    def test_compact_json_matches_section_8_separators(self):
        body = render_payload("2026-07-03T11:00:00+05:30", GUIDE_NUMBERS)
        self.assertEqual(body, GUIDE_BODY)
        self.assertEqual(json.dumps(json.loads(body), separators=(",", ":")), body)
        self.assertFalse(any(ch.isspace() for ch in body))

    def test_hmac_is_lowercase_hex_over_timestamp_dot_body(self):
        signature = sign_body(GUIDE_SECRET, GUIDE_TIMESTAMP, GUIDE_BODY)
        self.assertEqual(signature, GUIDE_DIGEST)
        self.assertEqual(signature, signature.lower())
        self.assertEqual(len(signature), 64)
        expected = hmac.new(
            GUIDE_SECRET.encode("utf-8"),
            signing_message(GUIDE_TIMESTAMP, GUIDE_BODY),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(signature, expected)
        self.assertNotEqual(sign_body(GUIDE_SECRET, GUIDE_TIMESTAMP, GUIDE_BODY + " "), signature)

    def test_auth_timestamp_is_unix_seconds_not_reading_at(self):
        self.assertEqual(unix_timestamp(1780656091.9), "1780656091")
        headers = auth_headers("HR_AR_0001_M_11", "1780656091", GUIDE_DIGEST)
        self.assertEqual(headers[HEADER_DEVICE], "HR_AR_0001_M_11")
        self.assertEqual(headers[HEADER_TIMESTAMP], "1780656091")
        self.assertEqual(headers[HEADER_SIGNATURE], GUIDE_DIGEST)
        self.assertNotIn("2026-07-03T11:00:00+05:30", headers[HEADER_TIMESTAMP])
        self.assertEqual(set(headers), {
            "X-SCADA-Device-Id",
            "X-SCADA-Timestamp",
            "X-SCADA-Signature",
            "Content-Type",
        })

    def test_batch_body_is_a_compact_array(self):
        body = assemble_body([GUIDE_BODY, GUIDE_BODY], "batch")
        self.assertEqual(body, f"[{GUIDE_BODY},{GUIDE_BODY}]")
        self.assertFalse(any(ch.isspace() for ch in body))
        self.assertEqual(assemble_body([GUIDE_BODY], "single"), GUIDE_BODY)

    def test_response_status_meanings(self):
        accepted = interpret_response(
            202,
            '{"status":"Accepted","message":"Telemetry batch queued successfully for processing",'
            '"batchId":"58616f2c-30c9-4b11-8b22-4c708fb0d6f5","processedCount":1}',
        )
        self.assertEqual(accepted.outcome, "accepted")
        self.assertEqual(accepted.batch_id, "58616f2c-30c9-4b11-8b22-4c708fb0d6f5")
        self.assertIsNone(accepted.message)
        rejected = interpret_response(
            400,
            '{"error":"Bad Request","message":"methane_mole_percent must be >= 95.0"}',
        )
        self.assertEqual(rejected.outcome, "bad_request")
        self.assertEqual(rejected.message, "methane_mole_percent must be >= 95.0")
        denied = interpret_response(
            401,
            '{"error":"Unauthorized","message":"Invalid SCADA request signature"}',
        )
        self.assertEqual(denied.outcome, "unauthorized")
        self.assertEqual(interpret_response(500, "nope").outcome, "transient")
        self.assertEqual(interpret_response(408, "").outcome, "timeout")
        self.assertEqual(interpret_response(200, '{"ok":true}').outcome, "uncertain")

    def test_local_post_sends_exact_header_names_and_body(self):
        captured = {}
        ready = threading.Event()

        def serve():
            server = socket.socket()
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            captured["port"] = server.getsockname()[1]
            ready.set()
            server.settimeout(5)
            try:
                connection, _addr = server.accept()
            except socket.timeout:
                captured["error"] = "accept timeout"
                server.close()
                return
            connection.settimeout(5)
            try:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                head, rest = data.split(b"\r\n\r\n", 1)
                text = head.decode("iso-8859-1")
                length = 0
                for line in text.split("\r\n")[1:]:
                    if line.lower().startswith("content-length:"):
                        length = int(line.split(":", 1)[1].strip())
                while len(rest) < length:
                    more = connection.recv(4096)
                    if not more:
                        break
                    rest += more
                captured["head"] = text
                captured["body"] = rest[:length]
                response = (
                    b'{"status":"Accepted","batchId":"batch-local-1",'
                    b'"message":"Telemetry batch queued successfully for processing","processedCount":1}'
                )
                raw = (
                    b"HTTP/1.1 202 Accepted\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: " + str(len(response)).encode("ascii") + b"\r\n"
                    b"Connection: close\r\n\r\n" + response
                )
                connection.sendall(raw)
            finally:
                connection.close()
                server.close()

        thread = threading.Thread(target=serve)
        thread.start()
        self.assertTrue(ready.wait(2))
        signature = sign_body(GUIDE_SECRET, GUIDE_TIMESTAMP, GUIDE_BODY)
        headers = auth_headers("HR_AR_0001_M_11", GUIDE_TIMESTAMP, signature)
        result = post_telemetry(
            f"http://127.0.0.1:{captured['port']}/api/v1/public/scada/telemetry",
            GUIDE_BODY,
            headers,
            3,
        )
        thread.join(5)
        self.assertEqual(result.outcome, "accepted", result.message)
        self.assertEqual(result.batch_id, "batch-local-1")
        self.assertEqual(captured["body"], GUIDE_BODY.encode("utf-8"))
        head = captured["head"]
        self.assertIn("POST /api/v1/public/scada/telemetry HTTP/1.1", head)
        self.assertIn("X-SCADA-Device-Id: HR_AR_0001_M_11", head)
        self.assertIn(f"X-SCADA-Timestamp: {GUIDE_TIMESTAMP}", head)
        self.assertIn(f"X-SCADA-Signature: {signature}", head)
        self.assertIn("Content-Type: application/json", head)
        self.assertNotIn("X-Scada-Device-Id", head)
        self.assertNotIn("X-scada-device-id", head)

    def test_connection_refused_is_not_treated_as_delivered(self):
        result = post_telemetry(
            "http://127.0.0.1:1/api/v1/public/scada/telemetry",
            "{}",
            {"Content-Type": "application/json"},
            1,
        )
        self.assertEqual(result.outcome, "connection")
        self.assertIsNone(result.http_status)


if __name__ == "__main__":
    unittest.main()
