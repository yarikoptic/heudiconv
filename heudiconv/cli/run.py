import os
import os.path as op
import logging
from argparse import ArgumentParser

# import processing pipeline

# TODO: set up logger


def main():
    args = get_parser().parse_args()
    if args.debug:
        logger.setLevel(logging.DEBUG)

    # error check some inputs
    if args.files and args.dicom_dir_template:
        raise ValueError("Specify files or dicom_dir_template, not both")

    if args.debug:
        setup_exceptionhook()

    # MG - why not just use args.overwrite

    # orig_global_options = global_options.copy()
    # try:
    #     global_options['overwrite'] = args.overwrite
    #     return _main(args)
    # finally:
    #     # reset back
    #     for k, v in orig_global_options.items():
    #         global_options[k] = v

    process_args(args)


def get_parser():
    docstr = '\n'.join((__doc__,
                        """
                                   Example:

                                   heudiconv -d rawdata/{subject} -o . -f
                                   heuristic.py -s s1 s2 s3
                        """))
    parser = ArgumentParser(description=docstr)
    parser.add_argument('--version', action='version', version=__version__)
    parser.add_argument('-d', '--dicom_dir_template',
                        dest='dicom_dir_template',
                        required=False,
                        help='''location of dicomdir that can be indexed with
                        subject id {subject} and session {session}.
                        Tarballs (can be compressed) are supported
                        in addition to directory. All matching tarballs for a
                        subject are extracted and their content processed in
                        a single pass''')
    parser.add_argument('-s', '--subjects', dest='subjs',
                        type=str, nargs='*',
                        help='list of subjects. If not provided, DICOMS would '
                             'first be "sorted" and subject IDs deduced by the '
                             'heuristic')
    parser.add_argument('-c', '--converter', default='dcm2niix',
                        choices=('dcm2niix', 'none'),
                        help='''tool to use for dicom conversion. Setting to
                        "none" disables the actual conversion step -- useful
                        for testing heuristics.''')
    parser.add_argument('-o', '--outdir', default=os.getcwd(),
                        help='''output directory for conversion setup (for
                        further customization and future reference. This
                        directory will refer to non-anonymized subject IDs''')
    parser.add_argument('-a', '--conv-outdir', default=None,
                        help='''output directory for converted files. By
                        default this is identical to --outdir. This option is
                        most useful in combination with --anon-cmd''')
    parser.add_argument('--anon-cmd', default=None,
                        help='''command to run to convert subject IDs used for
                        DICOMs to anonymmized IDs. Such command must take a
                        single argument and return a single anonymized ID.
                        Also see --conv-outdir''')
    parser.add_argument('-f', '--heuristic', dest='heuristic_file',
                        required=True,
                        help='python script containing heuristic')
    parser.add_argument('-q', '--queue', default=None,
                        help='''select batch system to submit jobs to instead
                        of running the conversion serially''')
    parser.add_argument('-p', '--with-prov', action='store_true',
                        help='''Store additional provenance information.
                        Requires python-rdflib.''')
    parser.add_argument('-ss', '--ses', dest='session', default=None,
                        help='''session for longitudinal study_sessions,
                        default is none''')
    parser.add_argument('-b', '--bids', action='store_true',
                        help='''flag for output into BIDS structure''')
    parser.add_argument('--overwrite', action='store_true', default=False,
                        help='''flag to allow overwrite existing files''')
    parser.add_argument('--datalad', action='store_true',
                        help='''Store the entire collection as DataLad
                        dataset(s). Small files will be committed directly to
                        git, while large to annex. New version (6) of annex
                        repositories will be used in a "thin" mode so it would
                        look to mortals as just any other regular directory
                        (i.e. no symlinks to under .git/annex).  For now just
                        for BIDS mode.''')
    parser.add_argument('--dbg', action='store_true', dest='debug',
                        help='''Do not catch exceptions and show
                        exception traceback''')
    parser.add_argument('--command',
                        choices=('treat-json', 'ls', 'populate-templates'),
                        help='''custom actions to be performed on provided
                        files instead of regular operation.''')
    parser.add_argument('-g', '--grouping',
                        default='studyUID',
                        choices=('studyUID', 'accession_number'),
                        help='''How to group dicoms (default: by studyUID)''')
    parser.add_argument('files', nargs='*',
                        help='''Files (tarballs, dicoms) or directories
                        containing files to process. Specify one of the
                        --dicom_dir_template or files (not both)''')
    parser.add_argument('--minmeta', action='store_true',
                        help='''Exclude dcmstack's meta information in
                        sidecar jsons''')
    return parser


def process_args(args):
    """Given a structure of arguments from the parser perform computation"""

    #
    # Deal with provided files or templates
    #

    #
    # pre-process provided list of files and possibly sort into groups/sessions
    #

    # Group files per each study/sid/session

    dicom_dir_template = args.dicom_dir_template
    files_opt = args.files
    session = args.session
    subjs = args.subjs
    outdir = os.path.abspath(args.outdir)
    grouping = args.grouping

    if args.command:
        # custom mode of operation
        if args.command == 'treat-json':
            for f in files_opt:
                treat_infofile(f)
        elif args.command == 'ls':
            heuristic = load_heuristic(os.path.realpath(args.heuristic_file))
            heuristic_ls = getattr(heuristic, 'ls', None)
            for f in files_opt:
                study_sessions = get_study_sessions(
                    dicom_dir_template, [f],
                    heuristic, outdir, session, subjs, grouping=grouping)
                print(f)
                for study_session, sequences in study_sessions.items():
                    suf = ''
                    if heuristic_ls:
                        suf += heuristic_ls(study_session, sequences)
                    print(
                        "\t%s %d sequences%s"
                        % (str(study_session), len(sequences), suf)
                    )
        elif args.command == 'populate-templates':
            heuristic = load_heuristic(os.path.realpath(args.heuristic_file))
            for f in files_opt:
                populate_bids_templates(
                    f,
                    getattr(heuristic, 'DEFAULT_FIELDS', {})
                )
        elif args.command == 'sanitize-jsons':
            tuneup_bids_json_files(files_opt)
        else:
            raise ValueError("Unknown command %s", args.command)
        return

    #
    # Load heuristic -- better do it asap to make sure it loads correctly
    #
    heuristic = load_heuristic(os.path.realpath(args.heuristic_file))
    # TODO: Move into a function!
    study_sessions = get_study_sessions(
        dicom_dir_template, files_opt,
        heuristic, outdir, session, subjs,
        grouping=grouping)
    # extract tarballs, and replace their entries with expanded lists of files
    # TODO: we might need to sort so sessions are ordered???
    lgr.info("Need to process %d study sessions", len(study_sessions))

    #
    # processed_studydirs = set()

    for (locator, session, sid), files_or_seqinfo in study_sessions.items():

        if not len(files_or_seqinfo):
            raise ValueError("nothing to process?")
        # that is how life is ATM :-/ since we don't do sorting if subj
        # template is provided
        if isinstance(files_or_seqinfo, dict):
            assert(isinstance(list(files_or_seqinfo.keys())[0], SeqInfo))
            dicoms = None
            seqinfo = files_or_seqinfo
        else:
            dicoms = files_or_seqinfo
            seqinfo = None

        if locator == 'unknown':
            lgr.warning("Skipping  unknown  locator dataset")
            continue

        if args.queue:
            if seqinfo and not dicoms:
                # flatten them all and provide into batching, which again
                # would group them... heh
                dicoms = sum(seqinfo.values(), [])
                # so
                raise NotImplementedError(
                    "we already groupped them so need to add a switch to avoid "
                    "any groupping, so no outdir prefix doubled etc"
                )
            # TODO This needs to be updated to better scale with additional args
            progname = os.path.abspath(inspect.getfile(inspect.currentframe()))
            convertcmd = ' '.join(['python', progname,
                                   '-o', study_outdir,
                                   '-f', heuristic.filename,
                                   '-s', sid,
                                   '--anon-cmd', args.anon_cmd,
                                   '-c', args.converter])
            if session:
                convertcmd += " --ses '%s'" % session
            if args.with_prov:
                convertcmd += " --with-prov"
            if args.bids:
                convertcmd += " --bids"
            convertcmd += ["'%s'" % f for f in dicoms]

            script_file = 'dicom-%s.sh' % sid
            with open(script_file, 'wt') as fp:
                fp.writelines(['#!/bin/bash\n', convertcmd])
            outcmd = 'sbatch -J dicom-%s -p %s -N1 -c2 --mem=20G %s' \
                     % (sid, args.queue, script_file)
            os.system(outcmd)
            continue

        anon_sid = get_annonimized_sid(sid, args.anon_cmd)

        study_outdir = opj(outdir, locator or '')

        anon_outdir = args.conv_outdir or outdir
        anon_study_outdir = opj(anon_outdir, locator or '')

        # TODO: --datalad  cmdline option, which would take care about initiating
        # the outdir -> study_outdir datasets if not yet there
        if args.datalad:
            datalad_msg_suf = ' %s' % anon_sid
            if session:
                datalad_msg_suf += ", session %s" % session
            if seqinfo:
                datalad_msg_suf += ", %d sequences" % len(seqinfo)
            datalad_msg_suf += ", %d dicoms" % (
                len(sum(seqinfo.values(), [])) if seqinfo else len(dicoms)
            )
            from datalad.api import Dataset
            ds = Dataset(anon_study_outdir)
            if not exists(anon_outdir) or not ds.is_installed():
                add_to_datalad(
                    anon_outdir, anon_study_outdir,
                    msg="Preparing for %s" % datalad_msg_suf,
                    bids=args.bids)
        lgr.info("PROCESSING STARTS: {0}".format(
            str(dict(subject=sid, outdir=study_outdir, session=session))))
        convert_dicoms(
                   sid,
                   dicoms,
                   study_outdir,
                   heuristic=heuristic,
                   converter=args.converter,
                   anon_sid=anon_sid,
                   anon_outdir=anon_study_outdir,
                   with_prov=args.with_prov,
                   ses=session,
                   is_bids=args.bids,
                   seqinfo=seqinfo,
                   min_meta=args.minmeta)
        lgr.info("PROCESSING DONE: {0}".format(
            str(dict(subject=sid, outdir=study_outdir, session=session))))

        if args.datalad:
            msg = "Converted subject %s" % datalad_msg_suf
            # TODO:  whenever propagate to supers work -- do just
            # ds.save(msg=msg)
            #  also in batch mode might fail since we have no locking ATM
            #  and theoretically no need actually to save entire study
            #  we just need that
            add_to_datalad(outdir, study_outdir, msg=msg, bids=args.bids)

    # if args.bids:
    #     # Let's populate BIDS templates for folks to take care about
    #     for study_outdir in processed_studydirs:
    #         populate_bids_templates(study_outdir)
    #
    #         # TODO: record_collection of the sid/session although that information
    #         # is pretty much present in .heudiconv/SUBJECT/info so we could just poke there

    tempdirs.cleanup()


if __name__ == "__main__":
    main()
