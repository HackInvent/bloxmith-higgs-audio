"""FB1/FB3/FB4/FB5: actual runtimes/packages/wallet, actual HTTP transport, synthetic model response."""

import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT),str(ROOT/"tests"),str(Path(__file__).parent)]
from blocs.higgs_audio.block import HiggsAudioBlock
from block_test_packages import install_test_package,prepare_release_run,surface_payload
from higgs_fixture import SECRET,settings,provider,wait as wait_process,check_file
from ui_smoke_common import (isolated_server,http_json,graph_payload,text_node,data_edge,create_project_api,
    graph_storage_dir,create_run_api,wait_for_run_terminal,wait_for_run_predicate,stop_run_api)


def value(data,port):
    raw=data.get("output_values",{}).get("tts:"+str(port),{}).get("value")
    return raw or "" if port==1 else json.loads(raw) if raw else {}


def main():
    for mode in ("centralized","zeromq_active"):
        for origin in (None,"managed","linked"):
            with isolated_server() as server,provider(server.root_dir) as backend:
                cfg=settings(backend)
                http_json(server.base_url,"/api/application/secrets/init",method="POST",payload={"password":"temporary-higgs-test"})
                http_json(server.base_url,"/api/application/secrets",method="POST",payload={"name":"higgs_fixture","value":SECRET})
                node=HiggsAudioBlock().build_node_payload(node_id="tts",position={"x":400,"y":160},config_overrides=cfg)
                node["outputs"].reverse();node["inputs"].reverse()
                if origin:
                    model=install_test_package(server,"higgs_audio",origin=origin);node["block_version"]=model["version"]
                    for surface in ("modal","inspector_panel","node_card"):
                        assert "{{" not in surface_payload(server,model,node,surface)["html"]
                graph=graph_payload("Higgs synthetic integration",[text_node("text","Text","",50,160),
                    text_node("command","Command","",50,360),node],
                    [data_edge("text-input","text",1,"tts",1)]+([data_edge("control","command",1,"tts",2)] if mode=="zeromq_active" else []))
                graph["nodes"][1]["outputs"][0]["emits"]=["application/json","message/*"]
                project=create_project_api(server,document=graph)["project"]
                storage=graph_storage_dir(server,project)/"instances"/"1"/"blocs"/"tts"
                requests=backend["requests"]
                if mode=="centralized":
                    def send(text):
                        graph["nodes"][0]["outputs"][0]["text"]=text
                        (graph_storage_dir(server,project)/"graph.json").write_text(json.dumps({**graph,"graph_id":project["graph_id"]}),encoding="utf-8")
                        http_json(server.base_url,f"/api/projects/{project['graph_id']}/graph/reload",method="POST",payload={})
                        run=create_run_api(server,graph,project_id=project["graph_id"],runtime_mode=mode)
                        data=wait_for_run_terminal(server,run["run_id"],timeout_sec=35)
                        assert data["status"]=="success",data.get("logs")
                        assert SECRET not in str(data)
                        return data
                    data=send("Example English speech")
                    assert value(data,3).get("code")=="completed",data.get("logs")
                    check_file(value(data,2));assert value(data,1)==value(data,2)["path"]
                    data=send("fixture:length")
                    assert value(data,3).get("code")=="audio_limit" and not value(data,1),data.get("logs")
                    assert value(data,3).get("request_id")
                    assert not list(storage.glob("higgs-job-*"))
                else:
                    rid=prepare_release_run(server,project["graph_id"],graph)["run_id"]
                    assert not requests,"Preparation launched inference"
                    http_json(server.base_url,f"/api/runs/{rid}/play",method="POST",payload={})
                    def send(source,payload):
                        http_json(server.base_url,f"/api/runs/{rid}/active/control",method="POST",payload={"action":"publish_output",
                            "node_id":source,"port_id":1,"value":json.dumps(payload),"content_type":"application/json"})
                    def wait(predicate):
                        data=wait_for_run_predicate(server,rid,lambda d:d.get("status")=="failed" or predicate(d),
                            "Higgs synthetic integration did not finish",timeout_sec=20)
                        assert data["status"]!="failed",data.get("logs")
                        assert SECRET not in str(data)
                        return data
                    def status(code,ident=None):
                        return wait(lambda d:value(d,3).get("code")==code and (ident is None or value(d,3).get("request_id")==ident))
                    stopped=False
                    try:
                        assert not requests,"Play alone launched inference"
                        send("text",{"text":"Hello","request_id":"first"})
                        data=wait(lambda d:value(d,3).get("code")=="completed" and value(d,2).get("request_id")=="first" and value(d,1)==value(d,2).get("path"))
                        original=check_file(value(data,2));files={original}
                        send("text",{"text":"fixture:hold","request_id":"held"})
                        wait_process(lambda:any(r["body"]["input"]=="fixture:hold" for r in requests))
                        send("text",{"text":"Hello queued","request_id":"queued"})
                        send("text",{"text":"Excess","request_id":"excess"})
                        status("queue_full","excess")
                        send("command",{"action":"interrupt"})
                        status("interrupted","held");wait_process(lambda:"fixture:hold" in backend["closed"])
                        assert not any(r["body"]["input"]=="Hello queued" for r in requests)
                        assert set((storage/"audio").iterdir())==files,"Interrupted audio was published"
                        send("text",{"text":"Hello again","request_id":"recovered"})
                        data=wait(lambda d:value(d,3).get("code")=="completed" and value(d,2).get("request_id")=="recovered" and value(d,1)==value(d,2).get("path"))
                        files.add(check_file(value(data,2)));assert len(files)==2
                        send("command",{"action":"invalid"});status("input")
                        assert len(requests)==3,"A command replayed remembered text"
                        http_json(server.base_url,"/api/application/secrets/lock",method="POST",payload={})
                        send("text",{"text":"No key","request_id":"locked"});status("credentials","locked")
                        http_json(server.base_url,"/api/application/secrets/unlock",method="POST",payload={"password":"temporary-higgs-test"})
                        send("text",{"text":"fixture:hold-stop","request_id":"stop"});wait_process(lambda:any(r["body"]["input"]=="fixture:hold-stop" for r in requests))
                        before=time.monotonic();stop_run_api(server,rid);ended=wait_for_run_terminal(server,rid,timeout_sec=15);stopped=True
                        assert time.monotonic()-before<8 and "shutdown_timeout" not in str(ended.get("logs")),ended.get("logs")
                        wait_process(lambda:"fixture:hold-stop" in backend["closed"]);assert set((storage/"audio").iterdir())==files
                        # Managed hosts may be forcibly terminated before Python's
                        # TemporaryDirectory finalizer. Only process death and no
                        # publication are guaranteed across abrupt host death; a
                        # private unfinished job directory can remain unreferenced.
                        if origin is None:assert not list(storage.glob("higgs-job-*"))
                    finally:
                        if not stopped:stop_run_api(server,rid);wait_for_run_terminal(server,rid,timeout_sec=15)
            print("[ok] Higgs synthetic speech HTTP protocol + actual wallet/runtime/package "+mode+" "+str(origin),flush=True)


if __name__=="__main__":main()
