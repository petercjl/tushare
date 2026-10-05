import unittest
import pandas as pd
from qt_research.reference_audit import audit


class ReferenceAuditTests(unittest.TestCase):
    def test_strict_and_diagnostic_policy_preserve_inputs(self):
        index = pd.to_datetime(['2020-02-25','2020-02-26','2020-02-27'])
        daily = pd.DataFrame(dict(close=[.942,1.,1.],volume=[13615918.,100.,100.],amount=[12787912.17,100.,100.]),index=index)
        reference = pd.DataFrame(dict(close=[.94,1.],vol=[117484.18,1.],amount=[11030.457,.1]),index=index[:2])
        before=daily.copy(deep=True)
        with self.assertRaises(ValueError): audit('159985.SZ',daily,reference)
        summary,issues=audit('159985.SZ',daily,reference,'warn')
        self.assertEqual(summary['reference_mismatch_days'],1)
        self.assertEqual(summary['reference_missing_days'],1)
        self.assertEqual(len(issues),2)
        self.assertIsNone(issues[1]['reference'])
        pd.testing.assert_frame_equal(daily,before)

    def test_standard_units_and_tolerance(self):
        idx=pd.to_datetime(['2020-01-02'])
        daily=pd.DataFrame(dict(close=[1.],volume=[100.],amount=[100.000001]),index=idx)
        reference=pd.DataFrame(dict(close=[1.],vol=[1.],amount=[.1]),index=idx)
        self.assertEqual(audit('test',daily,reference)[1],[])
        with self.assertRaises(ValueError): audit('test',daily,reference,'skip')
