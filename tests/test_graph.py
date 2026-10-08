import importlib.util
import json
from pathlib import Path
import subprocess
import shutil
import tempfile
import tarfile
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
            (root/'build.ninja').write_text('tool = printf \nrule action\n'
                '  command = ${tool}%s "$arg $$PWD" $in > $out && touch second\n'
                '  rspfile = $out.rsp\n  rspfile_content = $in_newline\n'
                'build first@path | second: action in@put input2 | hidden || order\n  arg = hello\n')
            db=root/'index.sqlite'
            graph.index_graph(root/'build.ninja',root,db)
            manifest=graph.Graph(db).slice(['first@path'],root/'.crux-task/graph')
            self.assertEqual(manifest['outputs'],['first@path','second'])
            self.assertEqual(manifest['edges'][0]['rspfile'],'first@path.rsp')
            self.assertEqual(manifest['edges'][0]['rspfile_content'],"'in@put'\ninput2")
            if not shutil.which('ninja'):
                self.skipTest('native Ninja executable is required for command validation')
            native=subprocess.run(['ninja','-f','build.ninja','-t','commands','first@path'],
                cwd=root,check=True,capture_output=True,text=True)
            sliced=subprocess.run(['ninja','-f','.crux-task/graph/build.ninja','-t','commands','first@path'],
                cwd=root,check=True,capture_output=True,text=True)
            self.assertEqual(native.stdout,sliced.stdout)
            self.assertEqual(native.stdout.strip(),manifest['commands'][0])
            for name in ('in@put','input2','hidden','order'):
                (root/name).write_text('source')
            actual=subprocess.run(['ninja','-f','build.ninja','-d','keeprsp','first@path'],
                cwd=root,check=True,capture_output=True,text=True)
            self.assertEqual((root/manifest['edges'][0]['rspfile']).read_text(),
                             manifest['edges'][0]['rspfile_content'])

    def test_reviewed_directory_owner_and_atomic_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'build.ninja').write_text('rule action\n  command = touch $out\n'
                'rule generate\n  command = mkdir -p gen && printf payload > gen/header && touch $out\n'
                'rule consume\n  command = cat gen/header > $out\n'
                'build mkdir: action\nbuild config: action mkdir\nbuild image: action config\n'
                'build stamp: generate input\nbuild final: consume gen/header image\n')
            (root/'input').write_text('source')
            db=root/'index.sqlite';graph.index_graph(root/'build.ninja',root,db)
            profile={'side_output_dirs':[{'producer':'stamp','dirs':['gen'],
                      'rationale':'generator stamp owns headers','evidence':['generator source:12']}],
                     'groups':[['mkdir','config','image']]}
            graph.augment_graph(db,profile)
            indexed=graph.Graph(db)
            manifest=indexed.slice(['final'],root/'.crux-task/graph')
            self.assertNotIn('gen/header',manifest['leaf_inputs'])
            self.assertEqual(manifest['output_dirs'],['gen'])
            self.assertIn('build stamp | gen: generate input',
                          (root/'.crux-task/graph/build.ninja').read_text())
            self.assertIn('mkdir -p gen && printf payload > gen/header && touch stamp',manifest['commands'])
            self.assertEqual(manifest['synthetic_aliases'],{'gen/header':'gen'})
            if shutil.which('ninja'):
                cold=subprocess.run(['ninja','-f','.crux-task/graph/build.ninja','final'],
                    cwd=root,capture_output=True,text=True)
                self.assertEqual(cold.returncode,0,cold.stderr)
                self.assertEqual((root/'final').read_text(),'payload')
            plan=indexed.shard(['final'],root/'shards',max_actions=1,max_parallel=2)
            groups=[j for wave in plan['waves'] for j in wave if j['action_count']==3]
            self.assertEqual(len(groups),1)
            self.assertEqual(set(groups[0]['targets']),{'mkdir','config','image'})
            self.assertIn('gen',plan['export_producers'])

    def test_ninja_validation_cycle_is_scheduled_and_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'build.ninja').write_text('rule action\n  command = touch $out\n'
                'build result: action input |@ checked\nbuild checked: action result\n')
            db=root/'index.sqlite';graph.index_graph(root/'build.ninja',root,db)
            indexed=graph.Graph(db)
            self.assertEqual(indexed.closure(['result'])['edge_count'],2)
            plan=indexed.shard(['result'],root/'shards',max_actions=1)
            self.assertEqual(plan['validation_outputs'],['checked'])
            self.assertEqual(plan['terminal_outputs'],['checked','result'])
            self.assertEqual(plan['wave_count'],2)
            first=plan['waves'][0][0]
            manifest=json.loads((root/'shards'/first['manifest']).read_text())
            self.assertEqual(manifest['deferred_validations'],[
                {'outputs':['result'],'validation':'checked'}])
            self.assertNotIn('|@',(root/'shards'/first['id']/'build.ninja').read_text())

    def test_resource_adaptation_is_checked_and_changes_only_emitted_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);original='rule kernel\n  command = make -j18 image\nbuild image: kernel\n'
            (root/'build.ninja').write_text(original)
            db=root/'index.sqlite';graph.index_graph(root/'build.ninja',root,db)
            adaptation={'command_adaptations':[{'producer':'image','replace':'-j18',
                'with':'-j4','expected_matches':1,'rationale':'runner resource allocation',
                'evidence':['kernel build rule:20']}]}
            graph.adapt_graph(db,adaptation)
            manifest=graph.Graph(db).slice(['image'],root/'.crux-task/graph')
            self.assertEqual(manifest['commands'],['make -j4 image'])
            self.assertEqual((root/'build.ninja').read_text(),original)
            adaptation['command_adaptations'][0]['expected_matches']=2
            with self.assertRaisesRegex(ValueError,'matches 1, expected 2'):
                graph.adapt_graph(db,adaptation)

    def test_generator_content_adaptation_is_limited_to_its_producer(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'build.ninja').write_text('rule write\n  command = printf %s $content > $out\n'
                "build manifest: write\n  content = 'make -j18'\n"
                "build other: write\n  content = 'make -j18'\n")
            db=root/'index.sqlite';graph.index_graph(root/'build.ninja',root,db)
            graph.adapt_graph(db,{'command_adaptations':[{'producer':'manifest','scope':'edge',
                'replace':'-j18','with':'-j4','expected_matches':1,
                'rationale':'runner allocation','evidence':['generator producer:20']}]})
            manifest=graph.Graph(db).slice(['manifest','other'],root/'.crux-task/graph')
            self.assertEqual(manifest['commands'],[
                "printf %s 'make -j4' > manifest","printf %s 'make -j18' > other"])

    def test_action_local_tool_binding_is_an_actual_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'build.ninja').write_text('rule tool\n  command = printf payload > $out\n'
                'rule generate\n  command = cat $cmd > $out\n'
                'build host-tool: tool\nbuild generated: generate | ${cmd}\n  cmd = host-tool\n'
                'build unrelated: phony ${cmd}\n')
            db=root/'index.sqlite';graph.index_graph(root/'build.ninja',root,db)
            indexed=graph.Graph(db)
            manifest=indexed.slice(['generated'],root/'.crux-task/graph')
            self.assertEqual(manifest['edge_count'],2)
            self.assertEqual(manifest['outputs'],['generated','host-tool'])
            self.assertEqual(manifest['leaf_inputs'],[])
            self.assertEqual(indexed.closure(['unrelated'])['edge_count'],1)
            if shutil.which('ninja'):
                cold=subprocess.run(['ninja','-f','.crux-task/graph/build.ninja','generated'],
                    cwd=root,capture_output=True,text=True)
                self.assertEqual(cold.returncode,0,cold.stderr)
                self.assertEqual((root/'generated').read_text(),'payload')

    def test_cold_exchange_preserves_broken_device_symlink(self):
        if not shutil.which('ninja'):
            self.skipTest('native Ninja executable is required for transport validation')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);producer=root/'producer';consumer=root/'consumer'
            producer.mkdir();consumer.mkdir()
            (producer/'build.ninja').write_text('rule link\n'
                '  command = ln -s /apex/com.android.runtime/lib64/libm.so $out\n'
                'rule install\n  command = cp -d $in $out\n'
                'build imported: link\nbuild installed: install imported\n')
            db=root/'index.sqlite';graph.index_graph(producer/'build.ninja',producer,db)
            subprocess.run(['ninja','-f','build.ninja','imported'],cwd=producer,check=True,
                           capture_output=True,text=True)
            self.assertTrue((producer/'imported').is_symlink())
            self.assertFalse((producer/'imported').exists())
            archive=root/'producer.tar'
            with tarfile.open(archive,'w') as output:
                output.add(producer/'imported',arcname='imported')
            with tarfile.open(archive) as imported:
                imported.extractall(consumer)
            self.assertEqual((consumer/'imported').readlink(),
                Path('/apex/com.android.runtime/lib64/libm.so'))
            manifest=graph.Graph(db).slice(['installed'],consumer/'.crux-task/graph',external=['imported'])
            self.assertEqual(manifest['external_phony_inputs'],['imported'])
            result=subprocess.run(['ninja','-f','.crux-task/graph/build.ninja','installed'],
                cwd=consumer,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertTrue((consumer/'installed').is_symlink())
            self.assertEqual((consumer/'installed').readlink(),(consumer/'imported').readlink())

    def test_aggregate_tree_does_not_replace_an_explicit_child_producer(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'build.ninja').write_text('rule action\n  command = mkdir -p root && touch $out\n'
                'build root/a: action\nbuild root/b: action root/a\n'
                'build stamp: action root/b\nbuild final: action stamp\n')
            db=root/'index.sqlite';graph.index_graph(root/'build.ninja',root,db)
            graph.augment_graph(db,{'side_output_dirs':[{'producer':'stamp','dirs':['root'],
                'rationale':'stamp aggregates completed tree','evidence':['aggregate recipe:20']}]})
            indexed=graph.Graph(db)
            manifest=indexed.slice(['root/a','root/b'],root/'.crux-task/graph',external=['root'])
            self.assertEqual(manifest['external_inputs'],[])
            self.assertEqual(manifest['external_phony_inputs'],[])
            self.assertEqual(manifest['outputs'],['root/a','root/b'])
            if shutil.which('ninja'):
                cold=subprocess.run(['ninja','-f','.crux-task/graph/build.ninja','root/b'],
                    cwd=root,capture_output=True,text=True)
                self.assertEqual(cold.returncode,0,cold.stderr)

    def test_stamp_export_transports_declared_header_cooutputs(self):
        if not shutil.which('ninja'):
            self.skipTest('native Ninja executable is required for co-output transport validation')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);producer=root/'producer';consumer=root/'consumer'
            producer.mkdir();consumer.mkdir()
            (producer/'build.ninja').write_text('rule generate\n'
                '  command = mkdir -p include && printf payload > include/generated.h && touch $out\n'
                'rule consume\n  command = cat include/generated.h > $out\n'
                'build stamp | include/generated.h: generate\nbuild result: consume stamp\n')
            db=root/'index.sqlite';graph.index_graph(producer/'build.ninja',producer,db)
            plan=graph.Graph(db).shard(['result'],root/'shards',max_actions=1)
            first=plan['waves'][0][0];second=plan['waves'][1][0]
            self.assertEqual(first['export_outputs'],['include/generated.h','stamp'])
            self.assertEqual(second['external_inputs'],['include/generated.h','stamp'])
            subprocess.run(['ninja','-f','build.ninja','stamp'],cwd=producer,check=True,
                           capture_output=True,text=True)
            archive=root/'outputs.tar'
            with tarfile.open(archive,'w') as output:
                for path in first['export_outputs']:
                    output.add(producer/path,arcname=path)
            with tarfile.open(archive) as outputs:outputs.extractall(consumer)
            shutil.copytree(root/'shards'/second['id'],consumer/'.crux-task/graph')
            capsule_path=MODULE.parent/'capsule.py'
            capsule_spec=importlib.util.spec_from_file_location('cooutput_capsule',capsule_path)
            capsule=importlib.util.module_from_spec(capsule_spec)
            capsule_spec.loader.exec_module(capsule)
            manifest=json.loads((consumer/'.crux-task/graph/manifest.json').read_text())
            collector=capsule.Collector(manifest,consumer,consumer/'include')
            header=consumer/'include/generated.h'
            self.assertTrue(collector.produced(header))
            collector.add(header,'existing-generated-header')
            collector.include_directory(consumer/'include','compiler-include-directory')
            self.assertEqual(collector.errors,set())
            self.assertNotIn(header,collector.entries)
            cold=subprocess.run(['ninja','-f','.crux-task/graph/build.ninja','result'],
                cwd=consumer,capture_output=True,text=True)
            self.assertEqual(cold.returncode,0,cold.stderr)
            self.assertEqual((consumer/'result').read_text(),'payload')


if __name__ == '__main__':
    unittest.main()
