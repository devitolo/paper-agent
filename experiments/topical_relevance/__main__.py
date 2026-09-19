"""Synthetic fixtures only; keyword/fake and pinned offline MiniLM smoke adapters."""
import argparse
import json
from pathlib import Path
from .contracts import ContractError, read_json
from . import dataset, freeze, runner, evaluate
from .supervisor import ExecutionFailure


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare')
    prepare.add_argument('--synthetic-input', type=Path, required=True)
    prepare.add_argument('--text-policy', type=Path, required=True)
    prepare.add_argument('--output', type=Path, required=True)
    check = commands.add_parser('verify'); check.add_argument('--manifest', type=Path, required=True)
    freezing = commands.add_parser('freeze')
    freezing.add_argument('--manifest', type=Path, required=True)
    freezing.add_argument('--config', type=Path, required=True)
    freezing.add_argument('--output', type=Path, required=True)
    for name in ('run', 'smoke'):
        cmd = commands.add_parser(name)
        cmd.add_argument('--freeze', type=Path, required=True)
        cmd.add_argument('--output', type=Path, required=True)
    resume = commands.add_parser('resume'); resume.add_argument('--run-dir', type=Path, required=True)
    blind = commands.add_parser('blind-sheet'); blind.add_argument('--manifest', type=Path, required=True); blind.add_argument('--output', type=Path, required=True)
    metrics = commands.add_parser('evaluate'); metrics.add_argument('--run-dir', type=Path, action='append', required=True)
    metrics.add_argument('--labels', type=Path, required=True); metrics.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            print(dataset.prepare(read_json(args.synthetic_input), read_json(args.text_policy), args.output))
        elif args.command == 'verify':
            freeze.validate_manifest(read_json(args.manifest));print('synthetic_manifest_verified')
        elif args.command == 'freeze':
            print(freeze.create(args.manifest,args.config,args.output))
        elif args.command in ('run','smoke'):
            if args.command == 'smoke':
                frozen = read_json(args.freeze)
                from .contracts import require
                require(len(frozen['manifest']['candidates']) <= 3, 'smoke_max_three_candidates')
            report = runner.run(args.freeze,output=args.output)
            print(json.dumps(report));return int(bool(report['counts']['failed'] or report['pending']))
        elif args.command == 'resume':
            report = runner.run(args.run_dir/'freeze.json',resume=args.run_dir)
            print(json.dumps(report));return int(bool(report['counts']['failed'] or report['pending']))
        elif args.command == 'blind-sheet':
            evaluate.blind_sheet(args.manifest,args.output)
        else:
            print(json.dumps(evaluate.evaluate(args.run_dir,args.labels,args.output)))
    except (ContractError, ExecutionFailure) as error:
        print(json.dumps({'status':'rejected','error_code':str(error)}));return 2
    except (OSError,ValueError,KeyError,TypeError):
        print(json.dumps({'status':'failed','error_code':'invalid_input_or_io'}));return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
