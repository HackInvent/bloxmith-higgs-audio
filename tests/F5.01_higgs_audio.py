"""FB1/FB2/FB3/FB4: actual HTTP/TLS/codecs and processes, synthetic server audio."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT),str(Path(__file__).parent)]
from blocs.higgs_audio import logic
from higgs_fixture import SECRET,provider,settings,wait,check_file


def refused(code, function, *args, **kwargs):
    try:
        function(*args, **kwargs)
    except logic.HiggsError as error:
        assert error.code==code,(error.code,code)
    else:
        raise AssertionError("Expected "+code)


def main():
    from test_listener import ListenerTests
    result=unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromTestCase(ListenerTests))
    assert result.wasSuccessful()
    cfg=logic.configuration({})
    assert cfg==logic.DEFAULTS
    for key,value in (("enabled",1),("format","mp3"),("api_key_ref","secret with spaces"),
            ("temperature",float("nan")),("temperature",True),("model_id",""),("voice",[]),("unknown",1)):
        refused("config",logic.configuration,{key:value})
    specs=json.loads((Path(__file__).parents[1]/"fields.json").read_text())
    for spec in specs:
        if spec.get("type") in {"integer","number"}:
            for bad in (spec["min"]-1,spec["max"]+1,True,"3"):
                refused("config",logic.configuration,{spec["key"]:bad})
    for url in ("file:///speech","http://example.org","http://localhost","https://u:p@example.org",
            "https://example.org/?token=value","https://example.org/#fragment","https://example.org/../escape",
            "https://example.org:0","https://example.org/%2e%2e","https://example.org/\n"):
        refused("config",logic.configuration,{"base_url":url})
    assert logic.endpoint("https://speech.example.org/proxy/")=="https://speech.example.org/proxy/v1/audio/speech"
    for text in ("", " "*3, "a"*4001, {"text":"ok"}, {"text":"ok","request_id":"a/b"}):
        refused("input",logic.request,text,cfg)
    assert logic.request('{"action":"interrupt"}',cfg)["text"].startswith("{")
    refused("input",logic.interrupt,{"action":"stop"})
    refused("input",logic.interrupt,'{"action":"interrupt","action":"interrupt"}')
    assert logic.interrupt('{"action":"interrupt"}')=={"action":"interrupt"}
    refused("credentials",logic.bearer_token,lambda _:SECRET+"\n","ref")
    references=[]
    assert logic.bearer_token(lambda ref: references.append(ref) or SECRET,"test")==SECRET
    assert references==["secret://workspace/test"]
    assert logic.configuration({"api_key_ref":"secret://project/project-id/key"})["api_key_ref"]
    refused("config",logic.configuration,{"api_key_ref":"secret://project/key"})

    with tempfile.TemporaryDirectory(prefix="higgs-tests-") as folder:
        root=Path(folder); storage=root/"storage"; storage.mkdir()
        with provider(root) as server:
            cfg=logic.configuration(settings(server))
            def generate(text,**changes):
                return logic.generate({"text":text,"request_id":"test-request"},{**cfg,**changes},root,storage,lambda _:SECRET)
            refused("disabled",generate,"not-sent",enabled=False)
            refused("credentials",logic.generate,{"text":"not-sent","request_id":"locked"},cfg,root,storage,None)
            assert server["requests"]==[]
            published=[]
            for text,fmt in (("ordinary text","wav"),("fixture:chunked","ogg_opus"),("fixture:no-length","wav")):
                prepared=generate(text,format=fmt)
                try:
                    assert not list((storage/"audio").glob("*")) if not published else True
                    metadata=logic.publish(prepared); published.append(check_file(metadata))
                    assert metadata["request_id"]=="test-request"
                finally:
                    prepared.close()
                request=server["requests"][-1]
                assert request["path"]=="/v1/audio/speech" and request["authorization"]=="Bearer "+SECRET
                assert request["body"]=={"model":cfg["model_id"],"voice":"default","input":text,"stream":False,
                    "response_format":"wav","speed":1.0,"temperature":.8,"top_k":50,"max_new_tokens":2048}
            for text,code in (("length","audio_limit"),("unknown","protocol"),("no-finish","protocol"),
                    ("duplicate","protocol"),("truncated","protocol"),("wrong-rate","audio_invalid"),
                    ("extra","audio_invalid"),("bad-wav","audio_invalid"),("mime","protocol"),
                    ("compressed","protocol"),("oversize","audio_limit"),("duration","audio_limit"),
                    ("auth","authentication"),("busy","unavailable"),("server","provider_failed"),("redirect","protocol")):
                before=len(server["requests"]);refused(code,generate,"fixture:"+text)
                assert len(server["requests"])==before+1,"Unexpected retry/redirect"
                assert not list(storage.glob("higgs-job-*"))
                assert set((storage/"audio").iterdir())==set(published)
            cancel=threading.Event()
            with ThreadPoolExecutor(max_workers=1) as pool:
                task=pool.submit(logic.generate,{"text":"fixture:hold-interrupt","request_id":"cancel"},cfg,root,storage,lambda _:SECRET,cancel.is_set)
                wait(lambda:any(r["body"]["input"]=="fixture:hold-interrupt" for r in server["requests"]))
                before=time.monotonic();cancel.set();refused("cancelled",task.result,timeout=3)
                assert time.monotonic()-before<2
            wait(lambda:"fixture:hold-interrupt" in server["closed"])
            refused("timeout",generate,"fixture:hold-timeout",timeout_sec=1)
            wait(lambda:"fixture:hold-timeout" in server["closed"])
            assert not list(storage.glob("higgs-job-*"))
            # Test the guardian with a real abruptly killed owner, not a process mock.
            script=root/"owner.py"
            script.write_text("import sys;sys.path.insert(0,"+repr(str(ROOT))+")\nfrom blocs.higgs_audio import logic\n"+
                "logic.generate({'text':'fixture:hold-parent','request_id':'parent'},"+repr(cfg)+","+repr(str(root))+","+repr(str(storage))+",lambda _:"+repr(SECRET)+")\n")
            owner=subprocess.Popen([sys.executable,"-B",str(script)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                wait(lambda:any(r["body"]["input"]=="fixture:hold-parent" for r in server["requests"]))
                owner.kill();owner.wait(timeout=3)
                wait(lambda:"fixture:hold-parent" in server["closed"])
                assert set((storage/"audio").iterdir())==set(published)
            finally:
                if owner.poll() is None:owner.kill();owner.wait(timeout=3)
                owner.stdout.close();owner.stderr.close()
        with provider(root,tls=True) as server:
            cfg=logic.configuration(settings(server))
            prepared=logic.generate({"text":"TLS tone","request_id":"tls"},cfg,root,storage,lambda _:SECRET)
            try:check_file(logic.publish(prepared))
            finally:prepared.close()
            refused("provider_failed",logic.generate,{"text":"Not trusted","request_id":"tls-refused"},
                {**cfg,"ca_bundle":""},root,storage,lambda _:SECRET)
            assert len(server["requests"])==1,"Untrusted TLS transmitted speech"
    print("[ok] Higgs actual HTTP/TLS, completion/framing gates, codecs, wallet privacy, interrupt/Stop/parent-death; synthetic tones")


if __name__=="__main__":main()
