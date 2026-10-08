import importlib.util
import json
from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1]/'scripts'/'graph.py'
spec = importlib.util.spec_from_file_location('graph',MODULE)
graph = importlib.util.module_from_spec(spec)
spec.loader.exec_module(graph)


class GraphTests(unittest.TestCase):
    def test_path_escapes_and_dependency_kinds(self):
        rule,outs,deps = graph.parse_build(
            'build dir/a$ b$:.o | side: cc src/a$ b.c | header || order |@ valid',{})
        self.assertEqual(rule,'cc')
        self.assertEqual(outs,['dir/a b:.o','side'])
        self.assertEqual(deps,[('src/a b.c','explicit'),('header','implicit'),
                              ('order','order'),('valid','validation')])
        self.assertEqual(graph.expand('$prefix/${name}$$',{'prefix':'x','name':'y'}),'x/y$')

    def test_sliced_graph_preserves_scopes_and_external_cut(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'root.ninja').write_text(
                'label = parent\nrule action\n  command = printf %s "$label" > $out\n'
                'build common: action input\nsubninja child.ninja\n'
                'build final: phony left right\n')
            (root/'child.ninja').write_text(
                'label = child\nbuild left: action common\nbuild right: action common\n')
            (root/'input').write_text('seed')
            db = root/'graph.sqlite'
            graph.index_graph(root/'root.ninja',root,db)
            indexed = graph.Graph(db)
            common = indexed.slice(['common'],root/'.crux-task/graph')
            self.assertEqual(common['leaf_inputs'],['input'])
            selected = indexed.slice(['final'],root/'.crux-task/graph',external=['common'])
            self.assertEqual(selected['outputs'],['left','right'])
            self.assertEqual(selected['external_inputs'],['common'])
            self.assertEqual(selected['leaf_inputs'],[])
            self.assertIn('label = parent',(root/'.crux-task/graph/build.ninja').read_text())
            self.assertIn('label = child',(root/'.crux-task/graph/file-2.ninja').read_text())
            self.assertEqual(selected['commands'],[
                'printf %s "child" > left','printf %s "child" > right'])
            if not shutil.which('ninja'):
                self.skipTest('native Ninja executable is required for execution validation')
            (root/'common').write_text('parent')
            result = subprocess.run(['ninja','-f','.crux-task/graph/build.ninja','final'],
                                    cwd=root,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual((root/'left').read_text(),'child')
            self.assertEqual((root/'right').read_text(),'child')

    def test_continuations_and_include_share_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'root.ninja').write_text('include vars.ninja\nrule cp\n  command = cp $in $out\n'
                'build $out: cp $\n  source$ file\n')
            (root/'vars.ninja').write_text('out = result\n')
            db=root/'index.sqlite'
            graph.index_graph(root/'root.ninja',root,db)
            closure=graph.Graph(db).closure(['result'])
            self.assertEqual(closure['leaf_inputs'],['source file'])

    def test_shards_have_no_dependencies_within_a_wave(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'build.ninja').write_text('rule cp\n  command = cp $in $out\n'
                'build a: cp input\nbuild b: cp a\nbuild c: cp a\n'
                'build alias: phony b c\nbuild d: cp alias\nbuild final: phony d\n')
            db=root/'index.sqlite'
            graph.index_graph(root/'build.ninja',root,db)
            plan=graph.Graph(db).shard(['final'],root/'shards',max_actions=1,max_parallel=2)
            self.assertEqual(plan['action_count'],4)
            self.assertEqual([len(w) for w in plan['waves']],[1,2,1])
            self.assertEqual(plan['terminal_outputs'],['d'])
            self.assertEqual(set(plan['export_producers']),{'a','b','c','d'})
            known=set()
            for wave in plan['waves']:
                for job in wave:
                    self.assertTrue(set(job['depends_on']) <= known)
                    manifest=json.loads((root/'shards'/job['manifest']).read_text())
                    self.assertEqual(len(manifest['outputs']),1)
                known.update(job['id'] for job in wave)

    def test_command_metadata_matches_native_ninja(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'build.ninja').write_text('tool = printf\nrule action\n'
                '  command = $tool %s "$arg $$PWD" $in > $out\n'
                '  rspfile = ${out}.rsp\n  rspfile_content = $in\n'
                'build first | second: action input | hidden || order\n  arg = hello\n')
            db=root/'index.sqlite'
            graph.index_graph(root/'build.ninja',root,db)
            manifest=graph.Graph(db).slice(['first'],root/'.crux-task/graph')
            self.assertEqual(manifest['outputs'],['first','second'])
            self.assertEqual(manifest['edges'][0]['rspfile'],'first.rsp')
            self.assertEqual(manifest['edges'][0]['rspfile_content'],'input')
            if not shutil.which('ninja'):
                self.skipTest('native Ninja executable is required for command validation')
            native=subprocess.run(['ninja','-f','build.ninja','-t','commands','first'],
                cwd=root,check=True,capture_output=True,text=True)
            sliced=subprocess.run(['ninja','-f','.crux-task/graph/build.ninja','-t','commands','first'],
                cwd=root,check=True,capture_output=True,text=True)
            self.assertEqual(native.stdout,sliced.stdout)
            self.assertEqual(native.stdout.strip(),manifest['commands'][0])


if __name__ == '__main__':
    unittest.main()
