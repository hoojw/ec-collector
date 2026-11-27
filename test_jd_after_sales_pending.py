import json
import unittest

import jd_after_sales_pending as collector


class FakeSocket:
    def __init__(self, messages):
        self.messages = [json.dumps(message) for message in messages]
        self.sent = []

    def send(self, payload):
        self.sent.append(json.loads(payload))

    def recv(self):
        return self.messages.pop(0)


class CdpEventQueueTest(unittest.TestCase):
    def test_call_preserves_network_events_that_precede_command_reply(self):
        event = {
            "method": "Network.requestWillBeSent",
            "params": {"requestId": "1", "request": {"url": "https://sff.jd.com/api"}},
        }
        page = object.__new__(collector.CdpPage)
        page.ws = FakeSocket([event, {"id": 1, "result": {"frameId": "frame"}}])
        page.message_id = 0
        page.events = []

        result = page.call("Page.navigate", {"url": collector.LIST_URL})

        self.assertEqual(result, {"frameId": "frame"})
        self.assertEqual(page.next_event(), event)
        self.assertEqual(page.events, [])


class ListDispatcherTest(unittest.TestCase):
    def test_loaded_micro_app_issues_the_list_request(self):
        page = object.__new__(collector.CdpPage)
        expressions = []
        page.evaluate = lambda expression: expressions.append(expression) or {"content": []}

        result = page.issue_operation(collector.LIST_OPERATION, {"tabCode": "all", "pageIndex": 1})

        self.assertEqual(result, {"content": []})
        self.assertIn("webpackChunkafter_sale", expressions[0])
        self.assertIn("getIndependentAfsListData", expressions[0])
        self.assertIn("operationFn", expressions[0])
        self.assertNotIn(".w;", expressions[0])
        self.assertIn('"pageIndex": 1', expressions[0])

    def test_next_event_reads_socket_when_queue_is_empty(self):
        event = {"method": "Page.loadEventFired", "params": {}}
        page = object.__new__(collector.CdpPage)
        page.ws = FakeSocket([event])
        page.events = []
        self.assertEqual(page.next_event(), event)


if __name__ == "__main__":
    unittest.main()
