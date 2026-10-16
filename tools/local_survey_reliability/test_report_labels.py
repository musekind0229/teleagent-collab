import unittest
from report_labels import localize_memory_ui_js


class ReportChromeTests(unittest.TestCase):
    def test_owned_script_translated_without_claim_data(self):
        value='for(let t of new Set(data.map(x=>x.kind))){o.textContent=t;} let x="${esc(x.kind)}";'
        translated=localize_memory_ui_js(value)
        self.assertIn('已保存的用户相关声明',translated)
        self.assertIn('kindLabels[t]||t',translated)
        self.assertIn('kindLabels[x.kind]||x.kind',translated)

    def test_retry_idempotent(self):
        value='for(let t of new Set(data.map(x=>x.kind))){}'
        once=localize_memory_ui_js(value)
        self.assertEqual(localize_memory_ui_js(once),once)


if __name__=='__main__':unittest.main()
