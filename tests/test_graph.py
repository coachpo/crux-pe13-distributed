import importlib.util
import json
from pathlib import Path
import subprocess
import shutil
import shlex
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

    def test_rust_transitive_search_inputs_survive_a_cold_shard_exchange(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);producer=root/'producer';consumer=root/'consumer'
            producer.mkdir();consumer.mkdir()
            fake_rust='''import argparse
from pathlib import Path
import shlex
import sys

expanded=[]
for arg in sys.argv[1:]:
    expanded.extend(shlex.split(Path(arg[1:]).read_text()) if arg.startswith('@') else [arg])
parser=argparse.ArgumentParser()
parser.add_argument('--crate-type')
parser.add_argument('-o', required=True)
parser.add_argument('--extern', action='append', default=[])
parser.add_argument('-L', action='append', default=[])
parser.add_argument('--source', required=True)
parser.add_argument('sources', nargs='*')
args=parser.parse_args(expanded)
externs=dict(value.split('=',1) for value in args.extern)
payload=Path(args.source).read_text()
if 'std' in externs:
    if not Path(externs['std']).read_text().startswith('requires=core\\n'):
        raise RuntimeError('invalid std crate metadata')
    core=next((Path(directory)/'libcore.rlib' for directory in args.L
               if (Path(directory)/'libcore.rlib').is_file()), None)
    if core is None:
        raise RuntimeError("cannot find core which std depends on")
    payload=core.read_text()
elif 'core' in externs:
    Path(externs['core']).read_text()
    payload='requires=core\\n'+payload
output=Path(args.o)
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(payload)
Path(args.o+'.d').write_text(args.o+': '+args.source+'\\n')
'''
            (producer/'fake_rust.py').write_text(fake_rust)
            for name in ('core','std','consumer'):
                (producer/(name+'.rs')).write_text(name+' payload')
            (producer/'build.ninja').write_text('rule g.rust.rustc\n'
                '  command = python3 fake_rust.py --crate-type=rlib -o $out $in $libraries @$out.rsp\n'
                '  depfile = $out.d\n  deps = gcc\n'
                '  rspfile = $out.rsp\n  rspfile_content = --source $in_newline\n'
                'build out/core/libcore.rlib: g.rust.rustc core.rs\n'
                'build out/std/libstd.rlib: g.rust.rustc std.rs | out/core/libcore.rlib\n'
                '  libraries = --extern core=out/core/libcore.rlib\n'
                'build out/consumer.rlib: g.rust.rustc consumer.rs | out/std/libstd.rlib\n'
                '  libraries = --extern std=out/std/libstd.rlib -L out/core\n')
            db=root/'index.sqlite';graph.index_graph(producer/'build.ninja',producer,db)
            indexed=graph.Graph(db)
            plan=indexed.shard(['out/consumer.rlib'],root/'shards',max_actions=1)
            jobs=[job for wave in plan['waves'] for job in wave]
            self.assertEqual(len(jobs),3)
            final=jobs[-1]
            self.assertEqual(final['external_inputs'],['out/core/libcore.rlib','out/std/libstd.rlib'])
            self.assertEqual(set(final['depends_on']),{jobs[0]['id'],jobs[1]['id']})
            self.assertIn('out/core/libcore.rlib',jobs[0]['export_outputs'])
            manifest=json.loads((root/'shards'/final['manifest']).read_text())
            self.assertEqual(manifest['external_phony_inputs'],final['external_inputs'])
            self.assertEqual(manifest['edges'][0]['depfile'],'out/consumer.rlib.d')
            self.assertEqual(manifest['edges'][0]['rspfile'],'out/consumer.rlib.rsp')
            self.assertEqual(manifest['edges'][0]['rspfile_content'],'--source consumer.rs')
            plan_only=indexed.shard(['out/consumer.rlib'],root/'plan-only',max_actions=1,export=False)
            normalize=lambda value:[{key:job[key] for key in
                ('id','depends_on','external_inputs','export_outputs')}
                for wave in value['waves'] for job in wave]
            self.assertEqual(normalize(plan_only),normalize(plan))
            local=indexed.shard(['out/consumer.rlib'],root/'local',max_actions=3,max_parallel=1)
            self.assertEqual(local['job_count'],1)
            self.assertEqual(local['waves'][0][0]['external_inputs'],[])
            if not shutil.which('ninja'):
                self.skipTest('native Ninja executable is required for cold Rust transport validation')
            native=subprocess.run(['ninja','-f','build.ninja','-t','commands','out/consumer.rlib'],
                cwd=producer,check=True,capture_output=True,text=True)
            exported_commands=[command for job in jobs
                for command in json.loads((root/'shards'/job['manifest']).read_text())['commands']]
            self.assertEqual(native.stdout.splitlines(),exported_commands)
            subprocess.run(['ninja','-f','build.ninja','out/std/libstd.rlib'],cwd=producer,
                check=True,capture_output=True,text=True)
            archive=root/'rust-outputs.tar'
            with tarfile.open(archive,'w') as output:
                for path in final['external_inputs']:
                    output.add(producer/path,arcname=path)
            with tarfile.open(archive) as output:output.extractall(consumer)
            for name in ('fake_rust.py','consumer.rs'):
                shutil.copy2(producer/name,consumer/name)
            shutil.copytree(root/'shards'/final['id'],consumer/'.crux-task/graph')
            sliced=subprocess.run(['ninja','-f','.crux-task/graph/build.ninja','-t','commands',
                'out/consumer.rlib'],cwd=consumer,check=True,capture_output=True,text=True)
            self.assertEqual(sliced.stdout.splitlines(),manifest['commands'])
            cold=subprocess.run(['ninja','-f','.crux-task/graph/build.ninja','out/consumer.rlib'],
                cwd=consumer,capture_output=True,text=True)
            self.assertEqual(cold.returncode,0,cold.stdout+cold.stderr)
            self.assertEqual((consumer/'out/consumer.rlib').read_text(),'core payload')

    def test_rust_search_contract_selects_crate_outputs_for_each_rust_consumer(self):
        for consumer_rule in ('g.rust.rustc','g.rust.clippy','g.rust.rustdoc'):
            with self.subTest(rule=consumer_rule), tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                rules=''.join('rule '+rule+'\n'
                    '  command = rust-tool --crate-type=$crate_type -o $out $in $libraries\n'
                    for rule in ('g.rust.rustc','g.rust.clippy','g.rust.rustdoc'))
                (root/'build.ninja').write_text(rules+
                    'rule metadata\n  command = touch $out\n'
                    'rule native\n  command = cxx -shared $in -o $out\n'
                    'build out/search/libcore.rlib: g.rust.rustc core.rs\n  crate_type = rlib\n'
                    'build out/search/libdependency.dylib.so: g.rust.rustc dependency.rs\n  crate_type = dylib\n'
                    'build out/search/libmacro.so: g.rust.rustc macro.rs\n  crate_type = proc-macro\n'
                    'build out/search/libcore.rmeta: g.rust.rustc core.rs\n  crate_type = rlib\n'
                    'build out/search/libcore.rlib.bloaty.csv: metadata\n'
                    'build out/search/meta_lic: metadata\n'
                    'build out/search/libnative.so: native native.cc\n'
                    'build out/search/libffi.so: g.rust.rustc ffi.rs\n  crate_type = cdylib\n'
                    'build out/std/libstd.rlib: g.rust.rustc std.rs | out/search/libcore.rlib '
                    'out/search/libdependency.dylib.so out/search/libmacro.so out/search/libcore.rmeta\n'
                    '  crate_type = rlib\n'
                    'build out/consumer.rlib: '+consumer_rule+' consumer.rs | out/std/libstd.rlib\n'
                    '  crate_type = rlib\n  libraries = -L out/search\n'
                    'build final: phony out/consumer.rlib out/search/libcore.rlib.bloaty.csv '
                    'out/search/meta_lic out/search/libnative.so out/search/libffi.so\n')
                db=root/'index.sqlite';graph.index_graph(root/'build.ninja',root,db)
                indexed=graph.Graph(db)
                plan=indexed.shard(['final'],root/'shards',max_actions=1,max_parallel=1)
                consumer=next(job for wave in plan['waves'] for job in wave
                    if 'out/consumer.rlib' in job['targets'])
                expected={'out/std/libstd.rlib','out/search/libcore.rlib',
                    'out/search/libdependency.dylib.so','out/search/libmacro.so','out/search/libcore.rmeta'}
                self.assertEqual(set(consumer['external_inputs']),expected)
                manifest=json.loads((root/'shards'/consumer['manifest']).read_text())
                self.assertEqual(set(manifest['external_phony_inputs']),expected)
                for path in expected:
                    owner=plan['export_producers'][path]
                    self.assertIn(owner,consumer['depends_on'])
                    producer=next(job for wave in plan['waves'] for job in wave if job['id']==owner)
                    self.assertIn(path,producer['export_outputs'])

    def test_retained_rust_std_keeps_local_core_ready_after_an_external_cut(self):
        if not shutil.which('ninja'):
            self.skipTest('native Ninja executable is required for cold Rust ordering validation')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);producer=root/'producer';consumer=root/'consumer'
            producer.mkdir();consumer.mkdir()
            (producer/'fake_rust.py').write_text('''import argparse
from pathlib import Path
import shlex
import sys

expanded=[]
for arg in sys.argv[1:]:
    expanded.extend(shlex.split(Path(arg[1:]).read_text()) if arg.startswith('@') else [arg])
parser=argparse.ArgumentParser()
parser.add_argument('--crate-type')
parser.add_argument('--sysroot')
parser.add_argument('-o', required=True)
parser.add_argument('--extern', action='append', default=[])
parser.add_argument('-L', action='append', default=[])
parser.add_argument('--source', required=True)
parser.add_argument('sources', nargs='*')
args=parser.parse_args(expanded)
if args.source == 'std.rs':
    raise RuntimeError('the retained std action must not run again')
payload=Path(args.source).read_text()
externs=dict(value.split('=',1) for value in args.extern)
if 'std' in externs:
    if Path(externs['std']).read_text() != 'requires=core\\nretained std':
        raise RuntimeError('invalid retained std metadata')
    core=next((Path(directory)/'libcore.rlib' for directory in args.L
               if (Path(directory)/'libcore.rlib').is_file()), None)
    if core is None:
        raise RuntimeError('cannot find local core which retained std depends on')
    payload=core.read_text()
output=Path(args.o)
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(payload)
Path(args.o+'.d').write_text(args.o+': '+args.source+'\\n')
''')
            for name in ('core','std','consumer'):
                (producer/(name+'.rs')).write_text(name+' source payload')
            (producer/'build.ninja').write_text('rule g.rust.rustc\n'
                '  command = python3 fake_rust.py --crate-type=rlib --sysroot=/dev/null '
                '-o $out $in $libraries @$out.rsp\n'
                '  depfile = $out.d\n  deps = gcc\n'
                '  rspfile = $out.rsp\n  rspfile_content = --source $in_newline\n'
                'build out/core/libcore.rlib: g.rust.rustc core.rs\n'
                'build out/std/libstd.rlib: g.rust.rustc std.rs | out/core/libcore.rlib\n'
                '  libraries = --extern core=out/core/libcore.rlib\n'
                'build out/consumer.rlib: g.rust.rustc consumer.rs | out/std/libstd.rlib\n'
                '  libraries = --extern std=out/std/libstd.rlib -L out/core\n')
            db=root/'index.sqlite';graph.index_graph(producer/'build.ninja',producer,db)
            indexed=graph.Graph(db)
            original=indexed.slice(['out/consumer.rlib'],root/'original-graph')
            retained=producer/'out/std/libstd.rlib'
            retained.parent.mkdir(parents=True)
            retained.write_text('requires=core\nretained std')
            archive=root/'retained-std.tar'
            with tarfile.open(archive,'w') as output:
                output.add(retained,arcname='out/std/libstd.rlib')
            with tarfile.open(archive) as output:
                self.assertEqual(output.getnames(),['out/std/libstd.rlib'])
                output.extractall(consumer)
            for name in ('fake_rust.py','core.rs','consumer.rs'):
                shutil.copy2(producer/name,consumer/name)
            self.assertFalse((consumer/'out/core/libcore.rlib').exists())
            manifest=indexed.slice(['out/core/libcore.rlib','out/consumer.rlib'],
                consumer/'.crux-task/graph',external=['out/std/libstd.rlib'])
            self.assertEqual(manifest['outputs'],['out/consumer.rlib','out/core/libcore.rlib'])
            self.assertEqual(manifest['external_inputs'],['out/std/libstd.rlib'])
            self.assertEqual(manifest['leaf_inputs'],['consumer.rs','core.rs'])
            original_edges={edge['outputs'][0]:edge for edge in original['edges']}
            for edge in manifest['edges']:
                before=original_edges[edge['outputs'][0]]
                for key in ('command','depfile','rspfile','rspfile_content'):
                    self.assertEqual(edge[key],before[key])
            native=subprocess.run(['ninja','-f','build.ninja','-t','commands','out/consumer.rlib'],
                cwd=producer,check=True,capture_output=True,text=True)
            self.assertEqual(native.stdout.splitlines(),original['commands'])
            query=subprocess.run(['ninja','-f','.crux-task/graph/build.ninja','-t','query',
                'out/consumer.rlib'],cwd=consumer,check=True,capture_output=True,text=True)
            self.assertIn('    || out/core/libcore.rlib\n',query.stdout)
            sliced=subprocess.run(['ninja','-f','.crux-task/graph/build.ninja','-t','commands',
                'out/consumer.rlib'],cwd=consumer,check=True,capture_output=True,text=True)
            self.assertEqual(sliced.stdout.splitlines(),manifest['commands'])
            cold=subprocess.run(['ninja','-f','.crux-task/graph/build.ninja','-j4','-d','keeprsp',
                'out/core/libcore.rlib','out/consumer.rlib'],cwd=consumer,capture_output=True,text=True)
            self.assertEqual(cold.returncode,0,cold.stdout+cold.stderr)
            self.assertEqual((consumer/'out/consumer.rlib').read_text(),'core source payload')
            self.assertEqual((consumer/'out/std/libstd.rlib').read_text(),'requires=core\nretained std')
            for edge in manifest['edges']:
                self.assertEqual((consumer/edge['rspfile']).read_text(),edge['rspfile_content'])

    def test_rust_search_rejects_a_local_crate_without_prerequisite_ordering(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'build.ninja').write_text('rule g.rust.rustc\n'
                '  command = rust-tool --crate-type=rlib -o $out $libraries\n'
                'build out/std/libstd.rlib: g.rust.rustc\n'
                'build out/consumer.rlib: g.rust.rustc out/std/libstd.rlib\n'
                '  libraries = -L out/search\n'
                'build out/search/liborphan.rlib: g.rust.rustc\n'
                'build final: phony out/consumer.rlib out/search/liborphan.rlib\n')
            db=root/'index.sqlite';graph.index_graph(root/'build.ninja',root,db)
            for export in (True,False):
                with self.subTest(export=export), self.assertRaisesRegex(ValueError,'ordering|ancestor|prerequisite'):
                    graph.Graph(db).shard(['final'],root/('export' if export else 'plan-only'),
                        max_actions=10,max_parallel=1,export=export)

    def test_native_include_operands_keep_their_executable_and_response_context(self):
        source='/fixture';out=source+'/out'
        edge={'command':"/bin/bash -c 'echo \"clang -c -Iout/data\"; env -i MODE=compile "
                        "ccache clang++ -c source.cc @out/compile.rsp'",
              'rspfile':'out/compile.rsp',
              'rspfile_content':'-Iout/include -isystem out/system -include out/config.h'}
        self.assertEqual(graph.native_generated_include_roots(edge,source,out),{
            'include_roots':[out+'/include',out+'/system'],'forced_files':[out+'/config.h']})
        abi={'command':'header-abi-dumper -Iout/filter source.cc -- -Iout/actual'}
        self.assertEqual(graph.native_generated_include_roots(abi,source,out),{
            'include_roots':[out+'/actual'],'forced_files':[]})
        for command in ('aproto -Iout/proto source.proto',
                        'build_license_metadata @out/data.rsp',
                        'header-abi-linker -Iout/filter @out/unknown.rsp',
                        'clang -shared @out/unknown.rsp -Iout/link-only'):
            with self.subTest(command=command):
                self.assertEqual(graph.native_generated_include_roots({'command':command},source,out),
                    {'include_roots':[],'forced_files':[]})
        with self.assertRaisesRegex(ValueError,'unknown native compiler response file'):
            graph.native_generated_include_roots({'command':'clang -c source.cc @unknown.rsp'},source,out)

    def test_generated_header_readiness_survives_a_cold_shard_exchange(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();source=root/'source';source.mkdir()
            (source/'main.c').write_text('#include <generated.h>\nint answer(void) { return GENERATED_VALUE; }\n')
            (source/'build.ninja').write_text('rule header\n'
                '  command = mkdir -p out/include && printf "#define GENERATED_VALUE 41\\n" > $out\n'
                'rule ready\n  command = touch $out\n'
                'rule g.cc.cc\n  command = cc -c $in -Iout/include -o $out -MMD -MF $out.d\n'
                '  depfile = $out.d\n  deps = gcc\n'
                'build out/include/generated.h: header\n'
                'build out/headers.timestamp: ready out/include/generated.h\n'
                'build out/main.o: g.cc.cc main.c || out/headers.timestamp\n')
            db=root/'index.sqlite';graph.index_graph(source/'build.ninja',source,db)
            indexed=graph.Graph(db)
            plan=indexed.shard(['out/main.o'],root/'shards',max_actions=1,out_root=source/'out')
            jobs=[j for wave in plan['waves'] for j in wave];consumer=jobs[-1]
            self.assertEqual(consumer['external_inputs'],['out/headers.timestamp','out/include/generated.h'])
            self.assertIn('out/include/generated.h',jobs[0]['export_outputs'])
            manifest=json.loads((root/'shards'/consumer['manifest']).read_text())
            contract=manifest['generated_include_contract']
            self.assertEqual(contract['search_roots'],[str(source/'out/include')])
            self.assertEqual(contract['required_headers'],['out/include/generated.h'])
            self.assertEqual(contract['owned_dirs'],[])
            self.assertEqual(contract['compiler_contexts'][0]['edge_index'],0)
            self.assertTrue(contract['provenance']['native_flags_unchanged'])
            self.assertEqual(manifest['edges'][0]['depfile'],'out/main.o.d')
            plan_only=indexed.shard(['out/main.o'],root/'plan-only',max_actions=1,export=False,
                                    out_root=source/'out')
            compare=lambda p:[{k:j[k] for k in ('targets','depends_on','external_inputs',
                'export_outputs','generated_include_contract')} for w in p['waves'] for j in w]
            self.assertEqual(compare(plan_only),compare(plan))
            if not shutil.which('ninja') or not shutil.which('cc'):
                self.skipTest('native Ninja and C compiler are required for cold header validation')
            native=subprocess.run(['ninja','-f','build.ninja','-t','commands','out/main.o'],
                cwd=source,check=True,capture_output=True,text=True)
            exported=[command for j in jobs for command in
                json.loads((root/'shards'/j['manifest']).read_text())['commands']]
            self.assertEqual(native.stdout.splitlines(),exported)
            subprocess.run(['ninja','-f','build.ninja','out/headers.timestamp'],cwd=source,
                check=True,capture_output=True,text=True)
            archive=root/'headers.tar'
            with tarfile.open(archive,'w') as output:
                for path in consumer['external_inputs']:output.add(source/path,arcname=path)
            # Recreate the recorded absolute source root, as independent workers do.
            source.rename(root/'previous-worker');source.mkdir()
            shutil.copy2(root/'previous-worker/main.c',source/'main.c')
            with tarfile.open(archive) as output:output.extractall(source)
            shutil.copytree(root/'shards'/consumer['id'],source/'.crux-task/graph')
            cold=subprocess.run(['ninja','-f','.crux-task/graph/build.ninja','out/main.o'],
                cwd=source,capture_output=True,text=True)
            self.assertEqual(cold.returncode,0,cold.stdout+cold.stderr)
            self.assertTrue((source/'out/main.o').is_file())

    def test_stub_include_flags_do_not_import_a_stale_or_future_header(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            (root/'build.ninja').write_text('rule stub\n'
                '  command = mkdir -p out/gen && printf "void symbol(void) {}\\n" > $out\n'
                'rule header\n  command = mkdir -p out/future && touch $out\n'
                'rule g.cc.cc\n  command = cc -c $in -Iout/future -o $out\n'
                'build out/gen/stub.c: stub\n'
                'build out/stub.o: g.cc.cc out/gen/stub.c\n'
                'build out/future/unrelated.h: header out/stub.o\n')
            (root/'out/future').mkdir(parents=True)
            (root/'out/future/old-unselected.h').write_text('old output must not become an input')
            db=root/'index.sqlite';graph.index_graph(root/'build.ninja',root,db)
            for targets in (['out/stub.o'],['out/future/unrelated.h']):
                with self.subTest(targets=targets):
                    destination=root/('unselected' if len(targets)==1 and targets[0]=='out/stub.o' else 'future')
                    plan=graph.Graph(db).shard(targets,destination,max_actions=1,out_root=root/'out')
                    job=next(j for wave in plan['waves'] for j in wave if 'out/stub.o' in j['targets'])
                    manifest=json.loads((destination/job['manifest']).read_text())
                    contract=manifest['generated_include_contract']
                    self.assertEqual(contract['search_roots'],[str(root/'out/future')])
                    self.assertEqual(contract['required_headers'],[])
                    self.assertEqual(contract['owned_dirs'],[])
                    self.assertEqual(job['external_inputs'],['out/gen/stub.c'])
            if shutil.which('ninja') and shutil.which('cc'):
                shutil.rmtree(root/'out/future')
                subprocess.run(['ninja','-f','build.ninja','out/gen/stub.c'],cwd=root,
                    check=True,capture_output=True,text=True)
                result=subprocess.run(['ninja','-f','unselected/'+job['id']+'/build.ninja','out/stub.o'],
                    cwd=root,capture_output=True,text=True)
                # The raw stub command tolerates the absent inherited include directory.
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_reviewed_generated_tree_is_bound_to_its_ancestor_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            (root/'build.ninja').write_text('rule generate\n'
                '  command = mkdir -p out/gen && printf payload > out/gen/generated.h && touch $out\n'
                'rule g.cc.cc\n  command = cc -c source.c -Iout/gen -o $out\n'
                'build out/stamp: generate\nbuild out/result.o: g.cc.cc || out/stamp\n')
            db=root/'index.sqlite';graph.index_graph(root/'build.ninja',root,db)
            graph.augment_graph(db,{'side_output_dirs':[{'producer':'out/stamp','dirs':['out/gen'],
                'rationale':'generator owns the complete generated include tree',
                'evidence':['generator recipe:1']}]})
            plan=graph.Graph(db).shard(['out/result.o'],root/'shards',max_actions=1,out_root=root/'out')
            producer,consumer=(wave[0] for wave in plan['waves'])
            self.assertIn('out/gen',producer['export_outputs'])
            self.assertEqual(consumer['external_inputs'],['out/gen','out/stamp'])
            self.assertEqual(consumer['generated_include_contract']['owned_dirs'],['out/gen'])

    def test_retained_header_stamp_keeps_a_local_header_ready_after_a_cut(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            (root/'main.c').write_text('#include <generated.h>\nint value(void) { return VALUE; }\n')
            (root/'build.ninja').write_text('rule header\n'
                '  command = mkdir -p out/include && printf "#define VALUE 7\\n" > $out\n'
                'rule ready\n  command = touch $out\n'
                'rule g.cc.cc\n  command = cc -c $in -Iout/include -o $out\n'
                'build out/include/generated.h: header\n'
                'build out/stamp: ready out/include/generated.h\n'
                'build out/result.o: g.cc.cc main.c || out/stamp\n')
            (root/'out').mkdir();(root/'out/stamp').write_text('retained readiness')
            db=root/'index.sqlite';graph.index_graph(root/'build.ninja',root,db)
            indexed=graph.Graph(db)
            manifest=indexed.slice(['out/result.o','out/include/generated.h'],root/'.crux-task/graph',
                external=['out/stamp'],out_root=root/'out')
            self.assertEqual(manifest['external_inputs'],['out/stamp'])
            self.assertEqual(manifest['generated_include_contract']['required_headers'],
                ['out/include/generated.h'])
            self.assertEqual(list(manifest['runtime_order_inputs'].values()),[['out/include/generated.h']])
            self.assertEqual(next(e for e in manifest['edges'] if e['rule']=='g.cc.cc')['command'],
                'cc -c main.c -Iout/include -o out/result.o')
            if shutil.which('ninja') and shutil.which('cc'):
                native=subprocess.run(['ninja','-f','.crux-task/graph/build.ninja','-j1',
                    'out/result.o','out/include/generated.h'],cwd=root,capture_output=True,text=True)
                self.assertEqual(native.returncode,0,native.stdout+native.stderr)

    def sbox_fixture(self, root, block, writer_rule='g.android.writeFile'):
        manifest=str(root/'out/gen.sbox.textproto');result=str(root/'out/result')
        (root/'out').mkdir(exist_ok=True)
        payload='commands:{'+block+'}\n'
        content=shlex.quote(payload.replace('\\','\\\\').replace('\n','\\n')).replace('$','$$')
        writer=' /bin/bash -c \'echo -e -n "$$0" > ${out}\' ${content}'
        if writer_rule != 'g.android.writeFile':writer=' cp $in $out'
        (root/'build.ninja').write_text(f'rule {writer_rule}\n  command ={writer}\n'
            f'rule generate\n  command = {root}/sbox --manifest $manifest\n'
            f'build {manifest}: {writer_rule}\n  content = {content}\n'
            f'build {result}: generate | {manifest} {root}/flatc-source\n  manifest = {manifest}\n')
        db=root/'index.sqlite';graph.index_graph(root/'build.ninja',root,db)
        return graph.Graph(db),manifest,result,payload

    def test_writefile_and_protobuf_escaping_are_separate_literal_layers(self):
        native='__SBOX_SANDBOX_DIR__/tools/flatc -I schemas "schemas/a b.fbs" && printf \'{command: decoy}\\n\''
        payload='commands:{command:'+json.dumps(native)+'}\n'
        literal=payload.replace('\\','\\\\').replace('\n','\\n')
        command="/bin/bash -c 'echo -e -n \"$0\" > output' "+shlex.quote(literal)
        saved,decoded=graph.soong_writefile_literal(command,'output')
        self.assertEqual(saved,literal)
        self.assertEqual(decoded,payload)
        self.assertEqual(graph.sbox_textproto(decoded)['commands'][0]['command'],[native])
        self.assertEqual(graph.sbox_textproto(r'''commands{command:"flat" 'c \xE4\xB8\xAD \uD83D\uDE00'}''')
                         ['commands'][0]['command'],['flatc 中 😀'])
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(['/bin/bash','-c','echo -e -n "$0" > output',literal],
                           cwd=directory,check=True)
            self.assertEqual((Path(directory)/'output').read_bytes(),payload.encode())
        for malformed in ('commands:{command:"unterminated}',r'commands:{command:"\q"}',
                          'commands:{command:"raw\nnewline"}','commands{command"missing colon"}'):
            with self.subTest(malformed=malformed),self.assertRaises(ValueError):
                graph.sbox_textproto(malformed)

    def test_embedded_flatc_consumer_survives_a_cold_external_manifest_cut(self):
        if not shutil.which('ninja'):
            self.skipTest('native Ninja executable is required for cold source capture validation')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();(root/'schemas').mkdir()
            (root/'schemas/main.fbs').write_text('include "nested.fbs";\nmain payload\n')
            (root/'schemas/nested.fbs').write_text('nested payload\n')
            (root/'flatc-source').write_text('#!/usr/bin/env python3\n'
                'import pathlib,re,sys\na=sys.argv[1:];p=pathlib.Path(a[-1]);data=p.read_text()\n'
                'for name in re.findall(r\'include "([^\\"]+)";\',data):\n'
                ' data+=(pathlib.Path(a[a.index("-I")+1])/name).read_text()\n'
                'pathlib.Path(a[a.index("-o")+1]).write_text(data)\n')
            (root/'flatc-source').chmod(0o755)
            (root/'sbox').write_text('#!/usr/bin/env python3\n'
                'import json,pathlib,re,shutil,subprocess,sys\n'
                'data=pathlib.Path(sys.argv[sys.argv.index("--manifest")+1]).read_text()\n'
                'p=pathlib.Path("sandbox");p.mkdir(exist_ok=True)\n'
                'for source,target in re.findall(r\'copy_before:\\{from:("(?:\\\\.|[^"\\\\])*") to:("(?:\\\\.|[^"\\\\])*")\',data):\n'
                ' d=p/json.loads(target);d.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(json.loads(source),d)\n'
                'command=json.loads(re.search(r\'command:("(?:\\\\.|[^"\\\\])*")\',data).group(1))\n'
                'subprocess.run(command.replace("__SBOX_SANDBOX_DIR__",str(p.resolve())),shell=True,check=True)\n')
            (root/'sbox').chmod(0o755)
            native=f'__SBOX_SANDBOX_DIR__/tools/bin/flatc -I schemas -o {root}/out/result schemas/main.fbs'
            block=(f'copy_before:{{from:{json.dumps(str(root/"flatc-source"))} '
                   f'to:"tools/bin/flatc" executable:false}} command:{json.dumps(native)}')
            indexed,manifest,result,payload=self.sbox_fixture(root,block)
            # Existing OUT payloads cannot supply capture metadata.
            Path(manifest).write_text('untrusted old payload without schema inputs')
            selected=indexed.slice([result],root/'.crux-task/graph',external=[manifest])
            context=selected['embedded_tool_contexts'][0]
            self.assertEqual(context['command'],native)
            self.assertEqual(context['edge_index'],0)
            self.assertEqual(context['producer_primary_output'],manifest)
            self.assertEqual(selected['external_inputs'],[manifest])
            original=subprocess.run(['ninja','-f','build.ninja','-t','commands',result],
                                    cwd=root,capture_output=True,text=True,check=True).stdout.splitlines()[-1]
            sliced=subprocess.run(['ninja','-f','.crux-task/graph/build.ninja','-t','commands',result],
                                  cwd=root,capture_output=True,text=True,check=True).stdout.strip()
            self.assertEqual(sliced,original)
            capsule_spec=importlib.util.spec_from_file_location('embedded_capsule',MODULE.parent/'capsule.py')
            capsule=importlib.util.module_from_spec(capsule_spec);capsule_spec.loader.exec_module(capsule)
            collector=capsule.Collector(selected,root,root/'out')
            for leaf in selected['leaf_inputs']:collector.add(leaf,'graph-leaf')
            collector.command(selected['commands'][0])
            for command in collector.embedded_commands():collector.command(command)
            self.assertEqual(collector.errors,set())
            self.assertIn(root/'schemas/nested.fbs',collector.entries)
            self.assertNotIn(Path(manifest),collector.entries)
            source_archive=root/'source.tar'
            with tarfile.open(source_archive,'w') as archive:
                for path in collector.entries:
                    archive.add(path,arcname=str(path.relative_to(root)),recursive=False)
            Path(manifest).unlink()
            subprocess.run(['ninja','-f','build.ninja',manifest],cwd=root,check=True,capture_output=True)
            self.assertEqual(Path(manifest).read_text(),payload)
            output_archive=root/'producer.tar'
            with tarfile.open(output_archive,'w') as archive:archive.add(manifest,arcname='out/gen.sbox.textproto')
            shutil.rmtree(root/'schemas');(root/'flatc-source').unlink();(root/'sbox').unlink();Path(manifest).unlink()
            (root/'.ninja_log').unlink(missing_ok=True)
            with tarfile.open(source_archive) as archive:archive.extractall(root)
            with tarfile.open(output_archive) as archive:archive.extractall(root)
            cold=subprocess.run(['ninja','-f','.crux-task/graph/build.ninja',result],
                                cwd=root,capture_output=True,text=True)
            self.assertEqual(cold.returncode,0,cold.stdout+cold.stderr)
            self.assertIn('nested payload',Path(result).read_text())
            exported=indexed.shard([result],root/'shards',max_actions=1)
            planned=indexed.shard([result],root/'planned',max_actions=1,export=False)
            self.assertEqual(exported['waves'][1][0]['embedded_tool_contexts'],
                             planned['waves'][1][0]['embedded_tool_contexts'])

    def test_embedded_flatc_rejects_unreviewed_sandbox_mappings(self):
        native='__SBOX_SANDBOX_DIR__/tools/flatc schemas/main.fbs'
        for extra in ('chdir:true','copy_before:{from:"schemas/main.fbs" to:"schemas/main.fbs"}',
                      'rsp_files:{file:"inputs.rsp"}','command:"echo duplicate"','unknown:true'):
            with self.subTest(extra=extra),tempfile.TemporaryDirectory() as directory:
                root=Path(directory).resolve()
                indexed,manifest,result,_=self.sbox_fixture(root,'command:'+json.dumps(native)+' '+extra)
                with self.assertRaises(ValueError):indexed.slice([result],root/'slice',external=[manifest])

    def test_unrelated_sbox_providers_are_excluded_before_flatc_validation(self):
        for rule,block in [('metalavaManifest','command:"metalava --api output"'),
                           ('g.android.writeFile','number:123 command:"echo unrelated"')]:
            with self.subTest(rule=rule),tempfile.TemporaryDirectory() as directory:
                root=Path(directory).resolve()
                indexed,manifest,result,_=self.sbox_fixture(root,block,rule)
                selected=indexed.slice([result],root/'slice',external=[manifest])
                self.assertEqual(selected['embedded_tool_contexts'],[])
                self.assertEqual(selected['external_inputs'],[manifest])

    def test_abi_namespace_flags_are_not_cpp_or_unrelated_include_operands(self):
        root='/source';out='/source/out'
        self.assertEqual(graph.native_abi_exported_roots({'command':
            'header-abi-linker -Iout/exports -I source/include -o out/api.lsdump input.sdump'},root,out),
            ['/source/out/exports'])
        for command in ('clang -c main.c -Iout/exports -o out/main.o',
                        'header-abi-dumper main.c -- -Iout/exports',
                        'echo "header-abi-linker -Iout/exports"'):
            self.assertEqual(graph.native_abi_exported_roots({'command':command},root,out),[])

    def test_abi_namespace_does_not_invent_a_stale_or_unordered_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();(root/'out/exports').mkdir(parents=True)
            (root/'out/exports/stale.h').write_text('old ambient header')
            (root/'build.ninja').write_text('rule generate\n  command = touch $out\n'
                'rule link\n  command = header-abi-linker -Iout/exports -o $out input.sdump\n'
                'build out/exports/future.h: generate\nbuild out/api.lsdump: link input.sdump\n')
            db=root/'index.sqlite';graph.index_graph(root/'build.ninja',root,db);indexed=graph.Graph(db)
            selected=indexed.slice(['out/api.lsdump'],root/'slice',out_root=root/'out')
            self.assertEqual(selected['abi_header_namespace_contract']['exported_roots'],
                             [str(root/'out/exports')])
            self.assertEqual(selected['abi_header_namespace_contract']['required_files'],[])
            self.assertEqual(selected['external_inputs'],[])
            with self.assertRaisesRegex(ValueError,'lacks original prerequisite ordering'):
                indexed.slice(['out/api.lsdump','out/exports/future.h'],root/'unordered',out_root=root/'out')

    def test_retained_abi_dump_keeps_a_locally_rebuilt_namespace_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            (root/'build.ninja').write_text('rule generate\n  command = mkdir -p out/exports && touch $out\n'
                'rule dump\n  command = touch $out\n'
                'rule link\n  command = header-abi-linker -Iout/exports -o $out out/input.sdump\n'
                'build out/exports/public.h: generate\nbuild out/input.sdump: dump out/exports/public.h\n'
                'build out/api.lsdump: link out/input.sdump\n')
            db=root/'index.sqlite';graph.index_graph(root/'build.ninja',root,db);indexed=graph.Graph(db)
            selected=indexed.slice(['out/api.lsdump','out/exports/public.h'],root/'slice',
                external=['out/input.sdump'],out_root=root/'out')
            self.assertEqual(selected['abi_header_namespace_contract']['required_files'],['out/exports/public.h'])
            self.assertEqual(list(selected['runtime_order_inputs'].values()),[['out/exports/public.h']])
            self.assertEqual(next(e['command'] for e in selected['edges'] if e['rule']=='link'),
                'header-abi-linker -Iout/exports -o out/api.lsdump out/input.sdump')

    def test_cold_native_abi_namespace_uses_real_declared_header_transport(self):
        tools=Path('/home/qingli/crux-pe13-offline-2026-09-25/pe13/prebuilts/clang-tools/linux-x86/bin')
        if not shutil.which('ninja') or not shutil.which('cc') or not (tools/'header-abi-linker').is_file():
            self.skipTest('native Ninja, C compiler and pinned ABI tools are required for cold validation')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();(root/'private').mkdir()
            (root/'private/private.h').write_text('int private_function(int x) { return x + 2; }\n')
            (root/'main.c').write_text('#include "public.h"\n#include "private.h"\n')
            (root/'build.ninja').write_text('rule header\n'
                '  command = mkdir -p out/exports && printf "int exported_function(int x) { return x + 1; }\\n" > $out\n'
                'rule compile\n  command = cc -shared -fPIC main.c -Iout/exports -Iprivate -o out/libfixture.so && '
                f'{tools}/header-abi-dumper main.c -o out/input.sdump --root-dir {root} -- -Iout/exports -Iprivate\n'
                'rule link\n  command = '
                f'{tools}/header-abi-linker --root-dir {root} -so out/libfixture.so -arch x86_64 '
                '-Iout/exports out/input.sdump -o $out\n'
                'build out/exports/public.h: header\n'
                'build out/libfixture.so | out/input.sdump: compile main.c out/exports/public.h\n'
                'build out/api.lsdump: link out/libfixture.so out/input.sdump\n')
            db=root/'index.sqlite';graph.index_graph(root/'build.ninja',root,db);indexed=graph.Graph(db)
            plan=indexed.shard(['out/api.lsdump'],root/'shards',max_actions=1,out_root=root/'out')
            consumer=plan['waves'][2][0];producer=plan['waves'][0][0]
            selected=json.loads((root/'shards'/consumer['manifest']).read_text())
            self.assertIn('out/exports/public.h',consumer['external_inputs'])
            self.assertIn('out/exports/public.h',producer['export_outputs'])
            self.assertEqual(selected['abi_header_namespace_contract']['required_files'],['out/exports/public.h'])
            planned=indexed.shard(['out/api.lsdump'],root/'planned',max_actions=1,export=False,out_root=root/'out')
            self.assertEqual(consumer['abi_header_namespace_contract'],planned['waves'][2][0]['abi_header_namespace_contract'])
            subprocess.run(['ninja','-f','build.ninja','out/api.lsdump'],cwd=root,check=True,capture_output=True)
            expected=(root/'out/api.lsdump').read_bytes();archive=root/'outputs.tar'
            with tarfile.open(archive,'w') as output:
                for wave in plan['waves'][:2]:
                    for job in wave:
                        for path in job['export_outputs']:output.add(root/path,arcname=path)
            shutil.rmtree(root/'out');(root/'.ninja_log').unlink(missing_ok=True)
            with tarfile.open(archive) as output:output.extractall(root)
            shutil.copytree(root/'shards'/consumer['id'],root/'.crux-task/graph')
            cold=subprocess.run(['ninja','-f','.crux-task/graph/build.ninja','out/api.lsdump'],
                                cwd=root,capture_output=True,text=True)
            self.assertEqual(cold.returncode,0,cold.stdout+cold.stderr)
            self.assertEqual((root/'out/api.lsdump').read_bytes(),expected)
            # A retained dump/library removes the original compile-to-header
            # chain. Rebuilding that header locally still precedes enumeration.
            (root/'out/exports/public.h').unlink();(root/'out/api.lsdump').unlink()
            local=indexed.slice(['out/api.lsdump','out/exports/public.h'],root/'.crux-task/graph',
                external=['out/libfixture.so','out/input.sdump'],out_root=root/'out')
            self.assertEqual(list(local['runtime_order_inputs'].values()),[['out/exports/public.h']])
            rebuilt=subprocess.run(['ninja','-f','.crux-task/graph/build.ninja','-j4',
                'out/api.lsdump','out/exports/public.h'],cwd=root,capture_output=True,text=True)
            self.assertEqual(rebuilt.returncode,0,rebuilt.stdout+rebuilt.stderr)
            self.assertEqual((root/'out/api.lsdump').read_bytes(),expected)


if __name__ == '__main__':
    unittest.main()
