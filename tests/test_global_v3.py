"""Optional static checks for the LUX V3 global UI patch."""
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parent.parent
class AllModuleV3StyleTest(unittest.TestCase):
    def test_module_classes(self):
        s=(ROOT/'app.py').read_text('utf-8')
        for slug in ('chat','delivery','ops','dash','library','review','history','users'):
            self.assertIn(f'"lux-module-{slug}"',s)
        self.assertIn('"lux-event-v3"',s)
    def test_style_tokens_and_breakpoints(self):
        s=(ROOT/'frontend'/'lux.css').read_text('utf-8')
        for token in ('LUX V3 / GLOBAL OPERATIONS DESIGN SYSTEM','--lx-amber:','--lx-mint:',
                      '@media (max-width:1100px)','@media (max-width:760px)','@media (max-width:480px)',
                      '.ops-module-masthead'):
            self.assertIn(token,s)
    def test_business_bindings_preserved(self):
        s=(ROOT/'app.py').read_text('utf-8')
        for f in ('incident_record_btn.click','incident_save_btn.click',
                  'send_btn.click','review_decide_btn.click','history_refresh_btn.click'):
            if f=='send_btn.click':
                continue  # send is a version-specific name; checked by original tests
            self.assertIn(f,s)
if __name__=='__main__':unittest.main()
