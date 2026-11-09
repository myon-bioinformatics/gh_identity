import sys, unittest
from pathlib import Path; from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import gh_identity as g


class IgnoreReuseTests(unittest.TestCase):
    def test_once_per_repository_and_same_filtering(self):
        class Names:
            calls=0
            def __iter__(self):
                self.calls+=1
                return iter(['bot'])
        names=Names()
        checks=[{'id':i,'name':'bot' if i%2 else 'test','status':'completed','conclusion':'success'} for i in range(100)]
        with patch.object(g,'remote_repository',return_value={'full_name':'o/r'}), \
             patch.object(g,'checkout_head',return_value={'sha':'a'*40}), \
             patch.object(g,'checks_for_sha',return_value={'checks':checks}) as fetch:
            result=g.fleet_checks(['one','two'],ignore=names)
        self.assertEqual(names.calls,2)
        self.assertEqual(fetch.call_count,2)
        self.assertEqual([r['count'] for r in result['rows']],[50,50])
        self.assertTrue(all(not r['failed'] for r in result['rows']))


if __name__=='__main__':unittest.main()
