import datetime as dt
import unittest
from direct_pipeline import automatic_refund_fields

class ProcessDeadlineTests(unittest.TestCase):
    def fields(self,deadline=1767225600,status='待商家审核',**extra):
        return automatic_refund_fields({'refundOrder':{'processDeadline':deadline,'b2cDisplayRefundStatusDesc':status,**extra}}, {})
    def test_deadline_without_countdown_copy(self):
        self.assertEqual(self.fields()['auto_refund_deadline'],dt.datetime(2026,1,1,8))
    def test_changed_copy_does_not_hide_deadline(self):
        self.assertIsNotNone(self.fields(countDown={'countDownText':'请及时处理'})['auto_refund_deadline'])
    def test_millisecond_timestamp(self):
        self.assertEqual(self.fields(1767225600000)['auto_refund_deadline'],dt.datetime(2026,1,1,8))
    def test_non_pending_states_are_excluded(self):
        for status in ['待商家收货','待买家寄回','退款成功','待商家审核,退款成功','']:
            with self.subTest(status=status):self.assertIsNone(self.fields(status=status)['auto_refund_deadline'])
    def test_interception_prefix_pending_review(self):
        self.assertIsNotNone(self.fields(status='拦截中,待商家审核')['auto_refund_deadline'])
    def test_bad_timestamps(self):
        for value in [None,'bad',0,-1,'nan','inf']:
            with self.subTest(value=value):self.assertIsNone(self.fields(value)['auto_refund_deadline'])
    def test_detail_deadline(self):
        result=automatic_refund_fields({}, {'data':{'refundDetailView':{'processDeadline':1767225600,'refundStatusDesc':'待商家审核'}}})
        self.assertEqual(result['auto_refund_deadline'],dt.datetime(2026,1,1,8))

if __name__=='__main__':unittest.main()
