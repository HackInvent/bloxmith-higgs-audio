"""Optional real-server qualification. Read-only planning unless --execute is explicit."""

import argparse
import getpass
import importlib
import json
from pathlib import Path
import sys

PACKAGE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PACKAGE.parent))
logic=importlib.import_module(PACKAGE.name+".logic")


def main():
    """Generate a bounded public example only on an administrator-selected server."""
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server",required=True,help="Explicit self-hosted root URL")
    parser.add_argument("--storage",type=Path,required=True,help="Existing disposable qualification directory")
    parser.add_argument("--ca-bundle",default="")
    parser.add_argument("--model",default="bosonai/higgs-tts-3-4b")
    parser.add_argument("--execute",action="store_true")
    parser.add_argument("--with-token",action="store_true",help="Read server token from a hidden terminal prompt")
    args=parser.parse_args()
    cfg=logic.configuration({"enabled":True,"base_url":args.server,"ca_bundle":args.ca_bundle,
        "model_id":args.model,"api_key_ref":"qualification" if args.with_token else "","max_audio_sec":30,
        "timeout_sec":240,"format":"ogg_opus"})
    print("Target contract: self-hosted Higgs speech API with X-Finish-Reason: stop.")
    print("The model/serving environment must already be installed and authorized.")
    if not args.execute:
        print("Plan only: no connection, secret read, model installation or directory creation.")
        return
    directory=args.storage.absolute()
    if not directory.is_dir() or directory.is_symlink():
        parser.error("Storage must be an existing real disposable directory.")
    token=getpass.getpass("Server bearer token: ") if args.with_token else ""
    if args.with_token and not token:
        parser.error("A non-empty token is required.")
    for identifier,text in (("english","Hello. This is a local Higgs speech synthesis test."),
            ("french","Bonjour. Ceci est un essai de synthèse vocale avec Higgs.")):
        prepared=None
        try:
            prepared=logic.generate({"text":text,"request_id":identifier},cfg,directory,directory,lambda _:token)
            result=logic.publish(prepared)
            report={"qualification":"real_server_file_protocol","language_requested":identifier,"result":result,
                "voice_quality_reviewed":False,"remote_model_revision_verified":False}
            report_path=directory/(Path(result["path"]).stem+".json")
            with report_path.open("x",encoding="utf-8") as output:
                json.dump(report,output,indent=2,ensure_ascii=True)
            print("Completed audio:",result["path"])
        except logic.HiggsError as error:
            print("Qualification failed:",error.code,file=sys.stderr)
            raise SystemExit(1) from None
        finally:
            if prepared is not None:prepared.close()


if __name__=="__main__":main()
