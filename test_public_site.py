import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import export_public_site


class PublicSiteTests(unittest.TestCase):
    def test_export_copies_only_referenced_assets_and_removes_process_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source';output=root/'output'
            (root/'web').mkdir();(root/'web/index.html').write_text('TB Science')
            (source/'data/models').mkdir(parents=True)
            (source/'data/models/current.gz').write_bytes(b'current')
            (source/'data/models/stale.gz').write_bytes(b'stale')
            (source/'data/runs.csv').write_text('trial_id\n')
            manifest={'files':{'task':{'parts':['models/current.gz']}},'counts':{'downloaded':1}}
            (source/'data/manifest.json').write_text(json.dumps(manifest))
            (output/'data/models').mkdir(parents=True)
            (output/'data/models/previous.gz').write_bytes(b'old')
            (output/'data/crawl-status.json').write_text('{"pid":123}')
            with patch.object(export_public_site,'HERE',root):
                result=export_public_site.export(source,output)
            self.assertEqual(result['counts'],{'downloaded':1})
            self.assertTrue(json.loads((output/'data/manifest.json').read_text())['public_snapshot'])
            self.assertEqual((output/'data/models/current.gz').read_bytes(),b'current')
            self.assertFalse((output/'data/models/previous.gz').exists())
            self.assertFalse((output/'data/models/stale.gz').exists())
            self.assertFalse((output/'data/crawl-status.json').exists())

    def test_export_refuses_an_asset_outside_the_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source';(source/'data').mkdir(parents=True)
            (root/'web').mkdir();(root/'web/index.html').write_text('TB Science')
            (source/'data/runs.csv').write_text('trial_id\n')
            (source/'data/manifest.json').write_text(json.dumps({'files':{'bad':{'parts':['../../outside']}},'counts':{}}))
            with patch.object(export_public_site,'HERE',root),self.assertRaisesRegex(ValueError,'escapes'):
                export_public_site.export(source,root/'output')

    def test_split_assets_reassemble_exactly_and_remove_old_unsplit_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source';output=root/'output'
            (root/'web').mkdir();(root/'web/index.html').write_text('TB Science')
            (source/'data/models').mkdir(parents=True)
            data=b'compressed-trajectory-bytes'
            (source/'data/models/current.gz').write_bytes(data)
            (source/'data/runs.csv').write_text('trial_id\n')
            (source/'data/manifest.json').write_text(json.dumps({'files':{'task':{'parts':['models/current.gz']}},'counts':{}}))
            (output/'data/models').mkdir(parents=True)
            (output/'data/models/current.gz').write_bytes(data)
            with patch.object(export_public_site,'HERE',root):
                export_public_site.export(source,output,max_part_bytes=7)
            parts=json.loads((output/'data/manifest.json').read_text())['files']['task']['parts']
            self.assertGreater(len(parts),1)
            self.assertEqual(b''.join((output/'data'/part).read_bytes() for part in parts),data)
            self.assertTrue(all((output/'data'/part).stat().st_size<=7 for part in parts))
            self.assertFalse((output/'data/models/current.gz').exists())


if __name__=='__main__':unittest.main()
