"""Complete self-hosted speech files with explicit interruption through the public listener."""

from collections import deque
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
from bloxsmith_app.block_api import BlockDefinition, BlockRuntimePreparation, BlockRuntimeOutput, BlockRuntimeResult
from . import logic, ui


def outcome(context, code, request_id="", metadata=None):
    """Return only typed outputs and owned diagnostics, independent of port order."""
    values={"status":{"code":code}}
    if code in {"interrupt_requested", "interrupted", "cancelled", "timeout"}:
        values["status"]["remote_cancel_confirmed"] = False
    if request_id: values["status"]["request_id"]=request_id
    if metadata is not None:
        values.update(audio_file=metadata["path"],metadata=metadata)
    outputs=[BlockRuntimeOutput(port_id=int(p.id),port_name=p.name,value=values[p.name] if p.name=="audio_file" else json.dumps(values[p.name],ensure_ascii=True,allow_nan=False),
        content_type="file/path" if p.name=="audio_file" else "application/json") for p in context.output_ports if p.name in values]
    return BlockRuntimeResult(status="success",outputs=outputs,metadata={"higgs_audio":values["status"]},logs=["[higgs-tts] "+code])


def failure(context,error,request_id=""):
    """Treat business failures as recoverable statuses without reflecting private errors."""
    code=error.code if isinstance(error,logic.HiggsError) else "provider_failed"
    result=outcome(context,code,request_id)
    # Safe owned messages only; no exception str from providers, file tools or wallet.
    from .process import ERRORS
    for output in result.outputs:
        if output.port_name=="status":
            value=json.loads(output.value);value["detail"]=ERRORS.get(code,ERRORS["provider_failed"]);output.value=json.dumps(value)
    return result


def delivered(context):
    """Read fresh events by stable port name; never replay remembered text on commands."""
    found={}
    if context.input_events:
        names={int(p.id):p.name for p in context.input_ports}
        for event in context.input_events:
            name=names.get(event.input_port_id)
            if name not in {"text","command"} or name in found: raise logic.HiggsError("input")
            value=event.value
            if name=="text" and event.content_type=="application/json":
                value=logic.json_value(value,16384)
            found[name]=value
    else:
        for port in context.input_ports:
            attribute=context.input_attribute(port.name,str(port.id))
            if attribute is not None and attribute.status=="updated":
                value=attribute.value
                if port.name=="text" and attribute.content_type=="application/json":
                    value=logic.json_value(value,16384)
                found[port.name]=value
    return found


# FB1 - Fresh bounded text and explicit commands; opt-in server and wallet-only credentials.
# FB2 - Official self-hosted Higgs speech API; finish-reason and complete WAV gates.
# FB3 - Bounded single-flight jobs, pending queue, priority interrupts and Stop cleanup.
# FB4 - Atomic unique file publication, source integrity, safe recoverable diagnostics.
# FB5 - Public runtime/package services and responsive translated draft settings.
class HiggsAudioBlock(BlockDefinition):
    kind="higgs_audio"

    def ui_assets(self,surface="modal"):
        return list(self.model.get("ui_assets",{}).get(surface,[]))

    def prepare_runtime(self,context):
        """Validate without IO; only Active Runtime has a command listener."""
        logic.configuration(context.config)
        return BlockRuntimePreparation(listen_on_run=context.runtime_mode=="zeromq_active")

    def execute_runtime(self,context):
        """Forward active jobs quickly, or synthesize synchronously in One Shot."""
        prepared=None;request_id=""
        try:
            cfg=logic.configuration(context.config);values=delivered(context)
            if not values: return BlockRuntimeResult(status="skipped",outputs=[])
            if "command" in values:
                message=logic.interrupt(values["command"])
            else:
                message={"action":"synthesize",**logic.request(values["text"],cfg)}
                request_id=message["request_id"]
            if context.runtime_mode=="zeromq_active":
                sender=context.services.get("runtime_listener")
                if sender is None: raise logic.HiggsError("listener")
                sender.send(message)
                return BlockRuntimeResult(status="success",outputs=[],metadata={self.kind:{"code":"submitted"}})
            if message["action"]=="interrupt": return outcome(context,"idle")
            resolver=context.services.get("get_block_storage_dir")
            if not callable(resolver): raise logic.HiggsError("storage")
            cancel=context.services.get("cancel_requested",lambda:False)
            item={k:message[k] for k in ("text","request_id")}
            prepared=logic.generate(item,cfg,context.root_dir,resolver(),context.services.get("resolve_secret"),cancel)
            metadata=logic.publish(prepared,cancel)
            return outcome(context,"completed",item["request_id"],metadata)
        except Exception as error:
            return failure(context,error,request_id)
        finally:
            if prepared is not None: prepared.close()

    def listen_runtime(self,context):
        """Own the bounded node queue, cancellation and complete-file publication fence."""
        cfg=logic.configuration(context.config);pending=deque();active=None
        pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix="higgs-owned")
        try:
            while not context.stop_requested():
                empty=False
                # Drain accepted interrupts before promoting any completed file.
                for _ in range(32):
                    command=context.receive_command(timeout_sec=0)
                    if command is None:
                        empty=True;break
                    message=command.payload
                    try:
                        if isinstance(message,Mapping) and dict(message)=={"action":"interrupt"}:
                            pending.clear()
                            if active is not None:
                                active["cancel"].set();active["interrupted"]=True
                                context.emit_result(outcome(context,"interrupt_requested",active["request_id"]))
                            else:
                                context.emit_result(outcome(context,"interrupted"))
                        elif isinstance(message,Mapping) and set(message)=={"action","text","request_id"} and message["action"]=="synthesize":
                            item=logic.request({k:message[k] for k in ("text","request_id")},cfg)
                            if len(pending)>=cfg["max_pending"]+(1 if active is None else 0):
                                context.emit_result(failure(context,logic.HiggsError("queue_full"),item["request_id"]))
                            else:
                                pending.append(item)
                        else: raise logic.HiggsError("input")
                    except Exception as error:
                        context.emit_result(failure(context,error))
                if context.stop_requested(): break
                if active is not None and active["future"].done() and empty:
                    prepared=None
                    try:
                        prepared=active["future"].result()
                        if active["interrupted"]:
                            context.emit_result(outcome(context,"interrupted",active["request_id"]))
                        else:
                            metadata=logic.publish(prepared,context.stop_requested)
                            context.emit_result(outcome(context,"completed",active["request_id"],metadata))
                    except Exception as error:
                        if active["interrupted"]: context.emit_result(outcome(context,"interrupted",active["request_id"]))
                        else: context.emit_result(failure(context,error,active["request_id"]))
                    finally:
                        if prepared is not None: prepared.close()
                        active=None
                if active is None and pending and empty and not context.stop_requested():
                    item=pending.popleft();cancel=threading.Event()
                    resolver=context.services.get("get_block_storage_dir")
                    if not callable(resolver):
                        context.emit_result(failure(context,logic.HiggsError("storage"),item["request_id"]))
                    else:
                        stop=lambda event=cancel:event.is_set() or context.stop_requested()
                        try:
                            storage=resolver()
                        except Exception:
                            context.emit_result(failure(context,logic.HiggsError("storage"),item["request_id"]))
                        else:
                            future=pool.submit(logic.generate,item,cfg,context.root_dir,storage,context.services.get("resolve_secret"),stop)
                            active={"future":future,"cancel":cancel,"request_id":item["request_id"],"interrupted":False}
                time.sleep(.02)
        finally:
            if active is not None:
                active["cancel"].set()
            pool.shutdown(wait=True,cancel_futures=True)
            if active is not None:
                try: active["future"].result().close()
                except Exception: pass

    def handle_ui_action(self,*,node,action,values,payload=None):
        if action in {"save_settings","modal_update_fields","inspector_update_fields"}:
            patch=(values or {}).get("node_patch") or {}
            if "config" in patch:
                try: logic.configuration({**self.default_config(),**(node.get("config") or {}),**patch["config"]})
                except (ValueError,TypeError):
                    return {"error":self.translate("block.higgs_audio.invalid_config",fallback="Check the Higgs TTS 3 settings and limits.")}
        return ui.replace_config(super().handle_ui_action(node=node,action="modal_update_fields" if action=="save_settings" else action,values=values,payload=payload),node)

    def render_modal(self,*,node,payload=None): return ui.modal(self,node,payload)

    def render_inspector_panel(self,*,node,payload=None): return ui.inspector(self,node,payload)

    def render_node_card(self,*,node,payload=None):
        cfg={**self.default_config(),**(node.get("config") or {})}
        return ui.card(self,node,cfg)
