import copy
import importlib.util
from pathlib import Path
import shlex
import sys
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('cloud_license_inputs', SCRIPTS / 'cloud_license_inputs.py')
license_inputs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(license_inputs)
source_reads = sys.modules['cloud_source_reads']


class LicenseMetadataReadTests(unittest.TestCase):
    source = '/SOURCE'
    out = '/OUT'

    def builder(self, output, dependencies=(), notices=(), response=False):
        arguments = [item for path in dependencies for item in ('-d', path + ':static')]
        arguments += [item for path in notices for item in ('-n', path + ':Android')]
        arguments += ['-r', 'descriptive/project', '-s', '/OUT/not-a-read.bin', '-t', '/OUT/not-a-read.img']
        command = '/OUT/host/bin/build_license_metadata -o ' + shlex.quote(output)
        edge = {'outputs': [output], 'command': command}
        if response:
            edge.update(command=command + ' @' + output + '.rsp', rspfile=output + '.rsp',
                        rspfile_content=shlex.join(arguments))
        else:
            edge['command'] += ' ' + shlex.join(arguments)
        return edge

    def alias(self, source, destination):
        script = '(rm -f ' + shlex.quote(destination) + ') && (cp ' + shlex.join([source, destination]) + ')'
        return {'outputs': [destination], 'command': '/bin/bash -c ' + shlex.quote(script)}

    def notice(self, *roots, program='xmlnotice', options=''):
        return {'outputs': ['/OUT/NOTICE.xml.gz'],
                'command': '/OUT/host/bin/' + program + ' -o /OUT/NOTICE.xml.gz ' + options
                + ' ' + shlex.join(roots)}

    def resolver(self, edges):
        return license_inputs.LicenseMetadataReads(edges, self.source, self.out)

    def test_direct_and_declared_go_response_builders_recurse(self):
        root = self.builder('/OUT/root.meta_lic', ['/OUT/child/meta_lic'], ['NOTICE root.txt'], response=True)
        child = self.builder('/OUT/child/meta_lic', notices=['child/NOTICE'])
        notice = self.notice('/OUT/root.meta_lic')
        self.assertEqual(self.resolver([root, child]).notice_reads(notice), {
            'metadata_inputs': ['/OUT/child/meta_lic', '/OUT/root.meta_lic'],
            'source_inputs': ['/SOURCE/NOTICE root.txt', '/SOURCE/child/NOTICE'],
        })

    def test_recorded_shell_script_reconstructs_make_arguments(self):
        output = '/OUT/vendor.img.meta_lic'
        arguments = '/OUT/notice_intermediates/arguments'
        payload = '-d /OUT/group_gen_odm/meta_lic:static -n "NOTICE root.txt:Android" '
        script = ' && '.join([
            '(rm -f ' + output + ')', '(mkdir -p /OUT/notice_intermediates)',
            '(rm -f ' + arguments + ')', '(touch ' + arguments + ')',
            '(echo -n ' + shlex.quote(payload) + ' >> ' + arguments + ')',
            '(OUT_DIR=/OUT /OUT/host/bin/build_license_metadata -mt raw -mc unknown '
            '-is_container -r vendor @' + arguments + ' -o ' + output + ')',
        ])
        root = {'outputs': [output], 'command': '/bin/bash ' + output + '.rsp',
                'rspfile': output + '.rsp', 'rspfile_content': script}
        child = self.builder('/OUT/group_gen_odm/meta_lic', ['/OUT/header/meta_lic'], ['build/soong/LICENSE'])
        header = self.builder('/OUT/header/meta_lic', notices=['headers/NOTICE'])
        before = copy.deepcopy([root, child, header])
        result = self.resolver([root, child, header]).notice_reads(self.notice(output))
        self.assertEqual(result, {
            'metadata_inputs': ['/OUT/group_gen_odm/meta_lic', '/OUT/header/meta_lic', output],
            'source_inputs': ['/SOURCE/NOTICE root.txt', '/SOURCE/build/soong/LICENSE', '/SOURCE/headers/NOTICE'],
        })
        self.assertEqual([root, child, header], before)

    def test_copy_provenance_is_not_an_additional_notice_read(self):
        original = self.builder('/OUT/original.meta_lic', ['/OUT/child.meta_lic'], ['NOTICE'])
        first = self.alias('/OUT/original.meta_lic', '/OUT/first.meta_lic')
        second = self.alias('/OUT/first.meta_lic', '/OUT/second.meta_lic')
        child = self.builder('/OUT/child.meta_lic', notices=['child/NOTICE'])
        resolver = self.resolver([original, first, second, child])
        self.assertEqual(resolver.notice_reads(self.notice('/OUT/second.meta_lic')), {
            'metadata_inputs': ['/OUT/child.meta_lic', '/OUT/second.meta_lic'],
            'source_inputs': ['/SOURCE/NOTICE', '/SOURCE/child/NOTICE'],
        })
        child_with_dependency = self.builder('/OUT/child.meta_lic', ['/OUT/original.meta_lic'])
        result = self.resolver([original, first, second, child_with_dependency]).notice_reads(
            self.notice('/OUT/second.meta_lic'))
        self.assertEqual(result['metadata_inputs'],
                         ['/OUT/child.meta_lic', '/OUT/original.meta_lic', '/OUT/second.meta_lic'])

    def test_copy_destination_and_origin_cwd_are_resolved_exactly(self):
        original = self.builder('/OUT/original.meta_lic', notices=['NOTICE'])
        original['command'] = 'cd /SOURCE/project && ' + original['command']
        alias = {'outputs': ['/OUT/copied.meta_lic'], 'command': '/bin/bash -c ' + shlex.quote(
            'cd /OUT/copies && cp /SOURCE/unrelated /OUT/unrelated && '
            'cp ../original.meta_lic ../copied.meta_lic')}
        result = self.resolver([original, alias]).notice_reads(self.notice('/OUT/copied.meta_lic'))
        self.assertEqual(result, {'metadata_inputs': ['/OUT/copied.meta_lic'],
                                  'source_inputs': ['/SOURCE/project/NOTICE']})

    def test_dependency_cycles_and_duplicate_roots_terminate(self):
        first = self.builder('/OUT/one.meta_lic', ['/OUT/two.meta_lic'], ['one/NOTICE'])
        second = self.builder('/OUT/two.meta_lic', ['/OUT/one.meta_lic'], ['two/NOTICE'])
        result = self.resolver([first, second]).notice_reads(self.notice(
            '/OUT/one.meta_lic', '/OUT/two.meta_lic', '/OUT/one.meta_lic'))
        self.assertEqual(result, {'metadata_inputs': ['/OUT/one.meta_lic', '/OUT/two.meta_lic'],
                                  'source_inputs': ['/SOURCE/one/NOTICE', '/SOURCE/two/NOTICE']})

    def test_copy_only_cycle_has_no_provable_payload(self):
        resolver = self.resolver([self.alias('/OUT/two.meta_lic', '/OUT/one.meta_lic'),
                                  self.alias('/OUT/one.meta_lic', '/OUT/two.meta_lic')])
        with self.assertRaisesRegex(ValueError, 'copy cycle'):
            resolver.notice_reads(self.notice('/OUT/one.meta_lic'))

    def test_go_response_expansion_is_single_pass(self):
        edge = self.builder('/OUT/root.meta_lic', response=True)
        edge['rspfile_content'] = '-n "@literal.txt:Android" -s @not-opened.rsp -r @not-opened-either'
        self.assertEqual(self.resolver([edge]).notice_reads(self.notice('/OUT/root.meta_lic')),
                         {'metadata_inputs': ['/OUT/root.meta_lic'], 'source_inputs': ['/SOURCE/@literal.txt']})

    def test_notice_response_and_option_roles_are_not_metadata_roots(self):
        root = self.builder('/OUT/root.meta_lic')
        notice = {'outputs': ['/OUT/NOTICE.html'], 'command': 'htmlnotice @/OUT/notice.rsp',
                  'rspfile': '/OUT/notice.rsp', 'rspfile_content':
                  '-o /OUT/output.meta_lic -d /OUT/depfile.meta_lic '
                  '--title "ignored.meta_lic" --product ignored.meta_lic '
                  '--strip_prefix /OUT/ignored.meta_lic /OUT/root.meta_lic'}
        self.assertEqual(self.resolver([root]).notice_reads(notice),
                         {'metadata_inputs': ['/OUT/root.meta_lic'], 'source_inputs': []})
        direct = self.notice('/OUT/root.meta_lic', options='-title="ignored.meta_lic"')
        direct['command'] += ' # hash of input list: ignored.meta_lic'
        self.assertEqual(self.resolver([root]).notice_reads(direct)['metadata_inputs'], ['/OUT/root.meta_lic'])

    def test_only_actual_notice_executables_participate(self):
        resolver = self.resolver([{'outputs': ['/OUT/unrelated$literal'], 'command': 'unrelated-tool'}])
        for command in ('echo "xmlnotice /OUT/missing.meta_lic"',
                        'xmlnotice-wrapper /OUT/missing.meta_lic',
                        'printf "unrelated malformed shell'):
            with self.subTest(command=command):
                self.assertIsNone(resolver.notice_reads({'command': command}))

    def test_unknown_generated_inputs_writers_and_transformations_fail(self):
        cases = [
            ([], 'no exact declared producer'),
            ([{'outputs': ['/OUT/root.meta_lic'], 'command': 'unknown-tool -o /OUT/root.meta_lic'}],
             'Unsupported generated'),
            ([self.builder('/OUT/original.meta_lic'),
              {'outputs': ['/OUT/root.meta_lic'], 'command':
               'cp /OUT/original.meta_lic /OUT/root.meta_lic && sed -i x /OUT/root.meta_lic'}],
             'Unsupported generated'),
            ([{'outputs': ['/OUT/root.meta_lic'], 'command':
               'build_license_metadata -o /OUT/root.meta_lic && '
               'echo "new protobuf fields" >> /OUT/root.meta_lic'}], 'output mutation'),
        ]
        for edges, error in cases:
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                self.resolver(edges).notice_reads(self.notice('/OUT/root.meta_lic'))

    def test_unrecorded_or_missing_response_and_script_payloads_fail(self):
        for edge, error in [
            ({'outputs': ['/OUT/root.meta_lic'], 'command': 'build_license_metadata -o /OUT/root.meta_lic @/OUT/unknown'},
             'Make response'),
            ({'outputs': ['/OUT/root.meta_lic'], 'command': 'build_license_metadata -o /OUT/root.meta_lic @/OUT/args',
              'rspfile': '/OUT/args'}, 'no recorded content'),
            ({'outputs': ['/OUT/root.meta_lic'], 'command': 'bash /OUT/unrecorded'}, 'unrecorded shell script'),
            ({'outputs': ['/OUT/root.meta_lic'], 'command': 'bash /OUT/script', 'rspfile': '/OUT/script'},
             'has no content'),
        ]:
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                self.resolver([edge]).notice_reads(self.notice('/OUT/root.meta_lic'))
        with self.assertRaisesRegex(ValueError, 'Unsupported notice response'):
            self.resolver([]).notice_reads({'command': 'xmlnotice @/OUT/unknown'})

    def test_source_metadata_and_dynamic_paths_are_not_guessed(self):
        for edge, error in [
            (self.alias('/SOURCE/old.meta_lic', '/OUT/root.meta_lic'), 'not a recorded OUT file'),
            (self.builder('/OUT/root.meta_lic', notices=['/OUT/compiled-notice']), 'outside SOURCE'),
            (self.builder('/OUT/root.meta_lic', dependencies=['$OUT/unknown.meta_lic']), 'Unresolved'),
            (self.builder('/OUT/root.meta_lic', notices=['licenses/*.txt']), 'Unresolved'),
        ]:
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                self.resolver([edge]).notice_reads(self.notice('/OUT/root.meta_lic'))

    def test_copy_chains_are_iterative_and_content_parsing_is_cached(self):
        edges = [self.builder('/OUT/0.meta_lic', notices=['NOTICE'])]
        for index in range(1, 1100):
            edges.append(self.alias('/OUT/' + str(index - 1) + '.meta_lic', '/OUT/' + str(index) + '.meta_lic'))
        resolver = self.resolver(edges)
        parser = license_inputs._shell_command_contexts
        with mock.patch.object(license_inputs, '_shell_command_contexts', wraps=parser) as contexts:
            first = resolver.notice_reads(self.notice('/OUT/1099.meta_lic'))
            second = resolver.notice_reads(self.notice('/OUT/1098.meta_lic'))
            self.assertLessEqual(contexts.call_count, len(edges) + 2)
        self.assertEqual(first, {'metadata_inputs': ['/OUT/1099.meta_lic'], 'source_inputs': ['/SOURCE/NOTICE']})
        self.assertEqual(second['metadata_inputs'], ['/OUT/1098.meta_lic'])
        first['metadata_inputs'].clear()
        self.assertEqual(resolver.notice_reads(self.notice('/OUT/1099.meta_lic'))['metadata_inputs'], ['/OUT/1099.meta_lic'])

    def test_recorded_script_parser_preserves_default_behavior_and_cwd(self):
        command = 'env -C /SOURCE/project bash /OUT/script.rsp'
        self.assertEqual(list(source_reads._shell_command_contexts(command, self.source)), [])
        content = '(cd nested && echo inside)\necho outside\n'
        contexts = list(source_reads._shell_command_contexts(command, self.source, {'/OUT/script.rsp': content}))
        self.assertEqual([(argv, cwd) for argv, cwd, _, _ in contexts],
                         [(['echo', 'inside'], '/SOURCE/project/nested'), (['echo', 'outside'], '/SOURCE/project')])

    def test_no_filesystem_probing_or_input_mutation(self):
        root = self.builder('/OUT/root.meta_lic', notices=['missing/NOTICE'], response=True)
        notice = self.notice('/OUT/root.meta_lic')
        before = copy.deepcopy([root, notice])
        with mock.patch('builtins.open', side_effect=AssertionError('filesystem read')), \
                mock.patch('os.stat', side_effect=AssertionError('filesystem stat')), \
                mock.patch('os.scandir', side_effect=AssertionError('filesystem enumeration')):
            result = self.resolver([root]).notice_reads(notice)
        self.assertEqual(result['source_inputs'], ['/SOURCE/missing/NOTICE'])
        self.assertEqual([root, notice], before)


if __name__ == '__main__':
    unittest.main()
