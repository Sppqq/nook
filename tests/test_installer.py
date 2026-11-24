import sys
sys.dont_write_bytecode=True
import contextlib, hashlib, importlib.util, io, json, os, shutil, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import urllib.error
spec=importlib.util.spec_from_file_location('installer', Path(__file__).resolve().parents[1] / 'install.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class InstallerTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.home=Path(self.tmp.name);self.target=self.home/'friend-bot'
  self.home_patch=patch.dict(os.environ,{'HOME':str(self.home)});self.home_patch.start();self.addCleanup(self.home_patch.stop)
  self.out=io.StringIO();self.redirect=contextlib.redirect_stdout(self.out);self.redirect.__enter__();self.addCleanup(self.redirect.__exit__,None,None,None)
 def response(self,value):return io.BytesIO(json.dumps(value).encode())
 def test_local_runtime_data_is_ignored_and_not_copied(self):
  source=self.home/'source';source.mkdir()
  files={'install.sh':'#!/bin/sh\n','mods/custom.py':'LOCAL=True\n'}
  for rel,data in files.items():
   path=source/rel;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(data)
  manifest={'format':1,'sha256':{rel:hashlib.sha256(data.encode()).hexdigest() for rel,data in files.items()}}
  (source/'MANIFEST.json').write_text(json.dumps(manifest))
  (source/'memory/notes.md').parent.mkdir(parents=True)
  (source/'memory/notes.md').write_text('private memory')
  (source/'config.json').write_text('private config')
  (source/'.env').write_text('private environment')
  (source/'.codex').mkdir()
  (source/'.codex/auth.json').write_text('private provider session')
  with patch.object(m,'SOURCE',source):
   m.validate(source)
   target=self.home/'installed'
   shutil.copytree(source,target,ignore=m.ignore_source_data)
  self.assertFalse((target/'memory/notes.md').exists())
  self.assertFalse((target/'config.json').exists())
  self.assertFalse((target/'.env').exists())
  self.assertFalse((target/'.codex/auth.json').exists())
  self.assertTrue((target/'mods/custom.py').is_file())
 def test_manifest_version_must_match_canonical_version(self):
  source=self.home/'version-source';source.mkdir()
  data='#!/bin/sh\n'
  (source/'install.sh').write_text(data)
  (source/'VERSION').write_text('1.2.3\n')
  hashes={name:hashlib.sha256(value.encode()).hexdigest() for name,value in
          {'install.sh':data,'VERSION':'1.2.3\n'}.items()}
  (source/'MANIFEST.json').write_text(json.dumps({'format':1,'version':'1.2.4','sha256':hashes}))
  with patch.object(m,'SOURCE',source),self.assertRaisesRegex(SystemExit,'VERSION'):
   m.validate(source)
 def test_terms_explicit_flag_and_yes_does_not_accept(self):
  with patch.object(m.sys.stdin,'isatty',return_value=False):
   m.accept_terms(SimpleNamespace(accept_terms=True,yes=True))
   with self.assertRaises(SystemExit):m.accept_terms(SimpleNamespace(accept_terms=False,yes=True))
 def test_terms_require_explicit_words(self):
  with patch.object(m.sys.stdin,'isatty',return_value=True):
   for answer in ('', 'yes', 'нет'):
    with patch('builtins.input',return_value=answer),self.assertRaises(SystemExit):
     m.accept_terms(SimpleNamespace(accept_terms=False))
   for answer in ('ПРИНИМАЮ', 'I ACCEPT'):
    with patch('builtins.input',return_value=answer):m.accept_terms(SimpleNamespace(accept_terms=False))
 def test_mercury_verified_secret_permissions(self):
  self.target.mkdir();(self.target/'secrets.env').write_text('OTHER=value\nINCEPTION_API_KEY=old\n')
  key='test-friend-mercury-secret'
  with patch.object(m.getpass,'getpass',return_value=key),patch.object(m.urllib.request,'urlopen',return_value=self.response({'choices':[{'message':{'content':'OK'}}]})) as net:
   self.assertTrue(m.setup_mercury(self.target))
  data=(self.target/'secrets.env').read_text();self.assertIn('OTHER=value',data);self.assertEqual(data.count('INCEPTION_API_KEY='),1)
  self.assertEqual((self.target/'secrets.env').stat().st_mode&0o777,0o600)
  self.assertNotIn(key,self.out.getvalue())
  req=net.call_args.args[0];self.assertEqual(json.loads(req.data)['model'],'mercury-3')
 def test_mercury_reject_does_not_write_or_leak(self):
  self.target.mkdir();key='invalid-friend-key-test'
  error=urllib.error.HTTPError('url',401,'test',{},None)
  with patch.object(m.getpass,'getpass',return_value=key),patch.object(m.urllib.request,'urlopen',side_effect=error),patch('builtins.input',return_value=''):
   self.assertFalse(m.setup_mercury(self.target))
  self.assertFalse((self.target/'secrets.env').exists());self.assertNotIn(key,self.out.getvalue())
 def test_antigravity_official_install(self):
  env=dict(os.environ,PATH='/usr/bin:/bin')
  def fake_run(args,**kwargs):
   self.assertEqual(args[0],'bash');self.assertEqual(args[2],'--dir')
   dest=Path(args[3]);dest.mkdir(parents=True);(dest/'agy').write_text('fake');(dest/'agy').chmod(0o755)
  with patch.object(m.shutil,'which',return_value=None),patch.object(m.urllib.request,'urlopen',return_value=io.BytesIO(b'#!/bin/bash\n')),patch.object(m,'run',side_effect=fake_run):
   binary=m.install_antigravity(env)
  self.assertEqual(binary,self.home/'.local/bin/agy')
 def test_all_three_login_sequence(self):
  args=SimpleNamespace(skip_codex=False,skip_antigravity=False,skip_mercury=False,mercury_key_file=None)
  def fake_subprocess(args,**kw):return SimpleNamespace(returncode=1 if 'status' in args else 0)
  with patch.object(m,'install_codex') as codex,patch.object(m,'install_antigravity',return_value=self.home/'agy') as agy,patch.object(m,'antigravity_ready',side_effect=[False,True]),patch.object(m.sys.stdin,'isatty',return_value=True),patch('builtins.input',return_value=''),patch.object(m.subprocess,'run',side_effect=fake_subprocess),patch.object(m,'run') as run,patch.object(m,'setup_mercury',return_value=True) as mercury:
   m.setup_providers(args,self.target,dict(os.environ))
   codex.assert_called_once();agy.assert_called_once();mercury.assert_called_once();run.assert_called_once()
   self.assertIn('--device-auth',run.call_args.args[0])
 def test_full_install_resume_and_nonoverwrite(self):
  tokenfile=self.home/'token';tokenfile.write_text('123456:'+('a'*35));calls=[]
  def fake_run(args,**kw):
   calls.append([str(a) for a in args])
   if len(args)>2 and args[1:3]==['-m','venv']:
    py=Path(args[3])/'bin/python';py.parent.mkdir(parents=True);py.write_text('mock-python')
  argv=['install.py','--accept-terms','--target',str(self.target),'--no-system-packages','--no-start','--owner-id','123456','--token-file',str(tokenfile),'--channel','alpha']
  with patch.object(m.os,'getuid',return_value=1001),patch.object(m.getpass,'getuser',return_value='friend'),patch.object(m,'run',side_effect=fake_run),patch.object(m,'setup_providers') as providers,patch.object(sys,'argv',argv):
   m.main();providers.assert_called_once()
   from nook_update import update_settings
   self.assertEqual(update_settings(self.target)['channel'],'alpha')
   cfg=(self.target/'config.json').read_bytes();self.assertEqual(json.loads(cfg)['owner_id'],123456)
   service=self.home/'.config/systemd/user/nook.service';self.assertIn(str(self.target),service.read_text())
   self.assertNotIn('start',[c[2] for c in calls if len(c)>2 and c[0]=='systemctl'])
   with self.assertRaises(SystemExit):m.main()
   self.assertEqual((self.target/'config.json').read_bytes(),cfg)
   with patch.object(sys,'argv',['install.py','--target',str(self.target),'--providers-only']):m.main()
   self.assertEqual(providers.call_count,2);self.assertEqual((self.target/'config.json').read_bytes(),cfg)
   # Finish-service may safely create only the missing unit after an interrupted login.
   service.unlink()
   with patch.object(sys,'argv',['install.py','--target',str(self.target),'--finish-service','--no-start']):m.main()
   self.assertTrue(service.is_file());self.assertEqual(providers.call_count,2)
   self.assertEqual((self.target/'config.json').read_bytes(),cfg)
   self.assertNotIn('__NOOK_HOME__', (self.target/'bot.py').read_text())
   self.assertTrue((self.target/'memory/notes.md').is_file())
   self.assertEqual(json.loads((self.target/'nodes.json').read_text()), [])
   # A Mercury-only installation must work on its first conversation.
   mercury_target=self.home/'mercury-only'
   mercury_argv=argv+['--skip-codex','--skip-antigravity','--target',str(mercury_target),'--channel','alpha']
   with patch.object(sys,'argv',mercury_argv),patch.object(m,'setup_service'):
    m.main()
   self.assertEqual(json.loads((mercury_target/'config.json').read_text())['model'],'mercury-3')
   self.assertEqual(update_settings(mercury_target)['channel'],'alpha')

class WizardTests(unittest.TestCase):
 def args(self, **kwargs):
  values=dict(yes=False,skip_codex=False,skip_antigravity=False,skip_mercury=False,voice=False,timezone='Europe/Moscow',no_start=True,github_account='skip')
  values.update(kwargs)
  return SimpleNamespace(**values)
 def test_presets_invalid_retry_and_voice(self):
  args=self.args()
  with patch.object(m.sys.stdin,'isatty',return_value=True),patch('builtins.input',side_effect=['bad','3','да']),contextlib.redirect_stdout(io.StringIO()):
   m.choose_options(args)
  self.assertTrue(args.skip_codex);self.assertTrue(args.skip_antigravity);self.assertFalse(args.skip_mercury);self.assertTrue(args.voice)
 def test_explicit_provider_flags_preserved(self):
  args=self.args(skip_antigravity=True,skip_mercury=True)
  with patch.object(m.sys.stdin,'isatty',return_value=True),patch('builtins.input',return_value='n') as prompt:
   m.choose_options(args)
   self.assertEqual(prompt.call_count,1);self.assertFalse(args.skip_codex)
 def test_cancel_plan_and_no_empty_provider_set(self):
  with patch.object(m.sys.stdin,'isatty',return_value=True),patch('builtins.input',return_value='n'),contextlib.redirect_stdout(io.StringIO()):
   with self.assertRaises(SystemExit):m.confirm_plan(self.args(),Path('/tmp/nook'))
  with self.assertRaises(SystemExit):m.confirm_plan(self.args(skip_codex=True,skip_antigravity=True,skip_mercury=True),Path('/tmp/nook'))
 def test_github_choice_and_explicit_flag(self):
  args=self.args(voice=True,skip_mercury=True,github_account=None)
  with patch.object(m.sys.stdin,'isatty',return_value=True),patch('builtins.input',side_effect=['bad','2']),contextlib.redirect_stdout(io.StringIO()):
   m.choose_options(args)
  self.assertEqual(args.github_account,'agent')
  with patch.object(m.sys.stdin,'isatty',return_value=True),patch('builtins.input') as prompt:
   m.choose_options(args)
  prompt.assert_not_called()
 def test_install_channel_and_version_selection(self):
  self.assertTrue(m.validate_update_selection('1.2.0','stable'))
  self.assertTrue(m.validate_update_selection('1.2.0-rc.1','beta','1.2.0-rc.1'))
  with self.assertRaisesRegex(ValueError,'не допускает'):
   m.validate_update_selection('1.2.0-alpha.1','stable')
  with self.assertRaisesRegex(ValueError,'совпадать'):
   m.validate_update_selection('1.2.0','stable','1.1.0')
 def test_update_channel_and_source_pin_prompts(self):
  args=SimpleNamespace(channel=None,version=None,yes=False)
  with patch.object(m.sys.stdin,'isatty',return_value=True),patch('builtins.input',side_effect=['3','yes']),contextlib.redirect_stdout(io.StringIO()):
   m.choose_update_settings(args,'1.2.0-beta.1')
  self.assertEqual(args.channel,'beta')
  self.assertEqual(args.version,'1.2.0-beta.1')
 def test_noninteractive_update_selection_defaults_to_stable_and_unpinned(self):
  args=SimpleNamespace(channel=None,version=None,yes=False)
  with patch.object(m.sys.stdin,'isatty',return_value=False):
   m.choose_update_settings(args,'1.2.0')
  self.assertEqual(args.channel,'stable')
  self.assertIsNone(args.version)

if __name__ == "__main__":
 unittest.main()
