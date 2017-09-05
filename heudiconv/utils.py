#!/usr/bin/env python

"""Convert DICOM dirs based on heuristic info

This script uses the dcmstack package and dcm2niix tool to convert DICOM
directories or tarballs into collections of NIfTI files following pre-defined
heuristic(s).

It has multiple modes of operation

- If subject ID(s) is specified, it proceeds by extracting dicominfo from each
  subject and writing a config file $subject_id/$subject_id.auto.txt in
  the output directory. Users can create a copy of the file called
  $subject_id.edit.txt and modify it to change the files that are
  converted. This edited file will always overwrite the original file. If
  there is a need to revert to original state, please delete this edit.txt
  file and rerun the conversion

- If no subject specified, all files under specified directory are scanned,
  DICOMs are sorted based on study UID, and layed out using specified heuristic
"""

__version__ = '0.3'

import argparse
from glob import glob
import csv
import dicom as dcm
import dcmstack as ds
import inspect
import json
import os
import sys
import re
import shutil
from tempfile import mkdtemp
import tarfile

from copy import deepcopy
from collections import namedtuple
from collections import defaultdict
from collections import OrderedDict as ordereddict
from datetime import datetime
from os.path import isdir
from os.path import basename
from os.path import dirname
from os.path import lexists, exists
from os.path import join as pjoin

from random import sample

PY3 = sys.version_info[0] >= 3

import logging
lgr = logging.getLogger('heudiconv')
# Rudimentary logging support.  If you want it better -- we need to have
# more than one file otherwise it is not manageable
logging.basicConfig(
    format='%(levelname)s: %(message)s',
    level=getattr(logging, os.environ.get('HEUDICONV_LOGLEVEL', 'INFO'))
)
lgr.debug("Starting the abomination")  # just to "run-test" logging

# bloody git screws things up
# https://github.com/gitpython-developers/GitPython/issues/600
#
## for nipype, we would like to lower level unless we are at debug level
#try:
#    import nipype
#    for l in nipype.logging.loggers.values():
#        if lgr.getEffectiveLevel() > 10:
#            l.setLevel(logging.WARN)
#except ImportError:
#    pass
#
#try:
#    # Set datalad's logger to our handler
#    import datalad
#    datalad_lgr = logging.getLogger('datalad')
#    datalad_lgr.handlers = lgr.handlers
#except ImportError:
#    pass

global_options = {
    'overwrite': False   # overwrite existing files
}

SeqInfo = namedtuple(
    'SeqInfo',
    ['total_files_till_now',  # 0
     'example_dcm_file',      # 1
     'series_id',             # 2
     'unspecified1',          # 3
     'unspecified2',          # 4
     'unspecified3',          # 5
     'dim1', 'dim2', 'dim3', 'dim4', # 6, 7, 8, 9
     'TR', 'TE',              # 10, 11
     'protocol_name',         # 12
     'is_motion_corrected',   # 13
     # Introduced with namedtuple
     'is_derived',
     'patient_id',
     'study_description',
     'referring_physician_name',
     'series_description',
     'sequence_name',
     'image_type',
     'accession_number',
     'patient_age',
     'patient_sex',
     'date'
     ]
)

StudySessionInfo = namedtuple(
    'StudySessionInfo',
    [
        'locator',  # possible prefix identifying the study, e.g. PI/dataset or just a dataset or empty (default)
                    # Note that ATM there should be no multiple DICOMs with the same
                    # StudyInstanceUID which would collide, i.e point to the same
                    # subject/session.
                    # So 'locator' is pretty much an assignment from StudyInstanceUID
                    # into some place within hierarchy
        'session',  # could be None
        'subject',  # should be some ID defined either in cmdline or deduced
    ]
)


# TODO: RF to avoid package-level global structure, and be more friendly in
# case of refactoring of heudiconv into a proper Python package/module
class TempDirs(object):
    """A helper to centralize handling and cleanup of dirs"""

    def __init__(self):
        self.dirs = []
        self.exists = os.path.exists

    def __call__(self, prefix=None):
        tmpdir = mkdtemp(prefix=prefix)
        self.dirs.append(tmpdir)
        return tmpdir

    def __del__(self):
        try:
            self.cleanup()
        except AttributeError:
            # we are too late to the show
            pass

    def cleanup(self):
        lgr.debug("Removing %d temporary directories", len(self.dirs))
        for t in self.dirs[:]:
            lgr.debug("Removing %s", t)
            if self:
                self.rmtree(t)
        self.dirs = []

    def rmtree(self, tmpdir):
        if self.exists(tmpdir):
            shutil.rmtree(tmpdir)
        if tmpdir in self.dirs:
            self.dirs.remove(tmpdir)

tempdirs = TempDirs()


def _canonical_dumps(json_obj, **kwargs):
    """ Dump `json_obj` to string, allowing for Python newline bug

    Runs ``json.dumps(json_obj, \*\*kwargs), then removes trailing whitespaces
    added when doing indent in some Python versions. See
    https://bugs.python.org/issue16333. Bug seems to be fixed in 3.4, for now
    fixing manually not only for aestetics but also to guarantee the same
    result across versions of Python.
    """
    out = json.dumps(json_obj, **kwargs)
    if 'indent' in kwargs:
        out = out.replace(' \n', '\n')
    return out


def save_json(filename, data):
    """Save data to a json file

    Parameters
    ----------
    filename : str
        Filename to save data in.
    data : dict
        Dictionary to save in json file.

    """
    with open(filename, 'w') as fp:
        fp.write(_canonical_dumps(data, sort_keys=True, indent=4))


def slim_down_info(j):
    """Given an aggregated info structure, removes excessive details

    such as Csa fields, and SourceImageSequence which on Siemens files could be
    huge and not providing any additional immediately usable information.
    If needed, could be recovered from stored DICOMs
    """
    j = deepcopy(j)  # we will do in-place modification on a copy
    dicts = []
    # poor man programming for now
    if 'const' in j.get('global', {}):
        dicts.append(j['global']['const'])
    if 'samples' in j.get('time', {}):
        dicts.append(j['time']['samples'])
    for d in dicts:
        for k in list(d.keys()):
            if k.startswith('Csa') or k.lower() in {'sourceimagesequence'}:
                del d[k]
    return j


def json_dumps_pretty(j, indent=2, sort_keys=True):
    """Given a json structure, pretty print it by colliding numeric arrays into a line

    If resultant structure differs from original -- throws exception
    """
    js = _canonical_dumps(j, indent=indent, sort_keys=sort_keys)
    # trim away \n and spaces between entries of numbers
    js_ = re.sub(
        '[\n ]+("?[-+.0-9e]+"?,?) *\n(?= *"?[-+.0-9e]+"?)', r' \1',
        js, flags=re.MULTILINE)
    # uniform no spaces before ]
    js_ = re.sub(" *\]", "]", js_)
    # uniform spacing before numbers
    js_ = re.sub('  *("?[-+.0-9e]+"?)[ \n]*', r' \1', js_)
    # no spaces after [
    js_ = re.sub('\[ ', '[', js_)
    j_ = json.loads(js_)
    assert(j == j_)
    return js_


def load_json(filename):
    """Load data from a json file

    Parameters
    ----------
    filename : str
        Filename to load data from.

    Returns
    -------
    data : dict

    """
    with open(filename, 'r') as fp:
        data = json.load(fp)
    return data


# They (https://github.com/nipy/heudiconv/issues/11#issuecomment-144665678)
# wanted it as a feature to have EVERYTHING in one file, so here you come
#

#
#  find_files utility copied/borrowed from DataLad (Copyright 2016 DataLad developers, MIT license)
#

from os.path import sep as dirsep
from os.path import curdir
from os.path import join as opj

_VCS_REGEX = '%s\.(?:git|gitattributes|svn|bzr|hg)(?:%s|$)' % (dirsep, dirsep)


def find_files(regex, topdir=curdir, exclude=None, exclude_vcs=True, dirs=False):
    """Generator to find files matching regex

    Parameters
    ----------
    regex: basestring
    exclude: basestring, optional
      Matches to exclude
    exclude_vcs:
      If True, excludes commonly known VCS subdirectories.  If string, used
      as regex to exclude those files (regex: `%r`)
    topdir: basestring, optional
      Directory where to search
    dirs: bool, optional
      Either to match directories as well as files
    """

    for dirpath, dirnames, filenames in os.walk(topdir):
        names = (dirnames + filenames) if dirs else filenames
        # TODO: might want to uniformize on windows to use '/'
        paths = (opj(dirpath, name) for name in names)
        for path in filter(re.compile(regex).search, paths):
            path = path.rstrip(dirsep)
            if exclude and re.search(exclude, path):
                continue
            if exclude_vcs and re.search(_VCS_REGEX, path):
                continue
            yield path
find_files.__doc__ %= (_VCS_REGEX,)


def group_dicoms_into_seqinfos(
        files, file_filter=None, dcmfilter=None, grouping='studyUID'
):
    """Process list of dicoms and return seqinfo and file group

    `seqinfo` contains per-sequence extract of fields from DICOMs which
    will be later provided into heuristics to decide on filenames

    Parameters
    ----------
    files : list of str
      List of files to consider
    file_filter : callable, optional
      Applied to each item of filenames. Should return True if file needs to be
      kept, False otherwise.
    dcmfilter : callable, optional
      If called on dcm_data and returns True, it is used to set series_id
    grouping : {'studyUID', 'accession_number', None}, optional
        what to group by: studyUID or accession_number

    Returns
    -------
    seqinfo : list of list
      `seqinfo` is a list of info entries per each sequence (some entry
      there defines a key for `filegrp`)
    filegrp : dict
      `filegrp` is a dictionary with files groupped per each sequence
    """
    allowed_groupings = ['studyUID', 'accession_number', None]
    if grouping not in allowed_groupings:
        raise ValueError('I do not know how to group by {0}'.format(grouping))
    per_studyUID = grouping == 'studyUID'
    per_accession_number = grouping == 'accession_number'
    lgr.info("Analyzing %d dicoms", len(files))
    import dcmstack as ds
    import dicom as dcm

    groups = [[], []]
    mwgroup = []

    studyUID = None
    # for sanity check that all DICOMs came from the same
    # "study".  If not -- what is the use-case? (interrupted acquisition?)
    # and how would then we deal with series numbers
    # which would differ already
    if file_filter:
        nfl_before = len(files)
        files = list(filter(file_filter, files))
        nfl_after = len(files)
        lgr.info('Filtering out {0} dicoms based on their filename'.format(
            nfl_before-nfl_after))
    for fidx, filename in enumerate(files):
        # TODO after getting a regression test check if the same behavior
        #      with stop_before_pixels=True
        mw = ds.wrapper_from_data(dcm.read_file(filename, force=True))

        for f in ('iop', 'ICE_Dims', 'SequenceName'):
            try:
                del mw.series_signature[f]
            except:
                pass

        try:
            file_studyUID = mw.dcm_data.StudyInstanceUID
        except AttributeError:
            lgr.info("File %s is missing any StudyInstanceUID" % filename)
            file_studyUID = None
            #continue

        try:
            series_id = (int(mw.dcm_data.SeriesNumber),
                         mw.dcm_data.ProtocolName)
            file_studyUID = mw.dcm_data.StudyInstanceUID

            if not per_studyUID:
                # verify that we are working with a single study
                if studyUID is None:
                    studyUID = file_studyUID
                elif not per_accession_number:
                    assert studyUID == file_studyUID
        except AttributeError as exc:
            lgr.warning('Ignoring %s since not quite a "normal" DICOM: %s',
                        filename, exc)
            # not a normal DICOM -> ignore
            series_id = (-1, 'none')
            file_studyUID = None

        if not series_id[0] < 0:
            if dcmfilter is not None and dcmfilter(mw.dcm_data):
                series_id = (-1, mw.dcm_data.ProtocolName)

        if not groups:
            raise RuntimeError("Yarik really thinks this is never ran!")
            # if I was wrong -- then per_studyUID might need to go above
            # yoh: I don't think this would ever be executed!
            mwgroup.append(mw)
            groups[0].append(series_id)
            groups[1].append(len(mwgroup) - 1)
            continue

        # filter out unwanted non-image-data DICOMs by assigning
        # a series number < 0 (see test below)
        if not series_id[0] < 0 and mw.dcm_data[0x0008, 0x0016].repval in (
                'Raw Data Storage',
                'GrayscaleSoftcopyPresentationStateStorage'):
            series_id = (-1, mw.dcm_data.ProtocolName)

        if per_studyUID:
            series_id = series_id + (file_studyUID,)


        #print fidx, N, filename
        ingrp = False
        for idx in range(len(mwgroup)):
            same = mw.is_same_series(mwgroup[idx])
            #print idx, same, groups[idx][0]
            if same:
                # the same series should have the same study uuid
                assert mwgroup[idx].dcm_data.get('StudyInstanceUID', None) == file_studyUID
                ingrp = True
                if series_id[0] >= 0:
                    series_id = (mwgroup[idx].dcm_data.SeriesNumber,
                                 mwgroup[idx].dcm_data.ProtocolName)
                    if per_studyUID:
                        series_id = series_id + (file_studyUID,)
                groups[0].append(series_id)
                groups[1].append(idx)

        if not ingrp:
            mwgroup.append(mw)
            groups[0].append(series_id)
            groups[1].append(len(mwgroup) - 1)

    group_map = dict(zip(groups[0], groups[1]))

    total = 0
    seqinfo = ordereddict()

    # for the next line to make any sense the series_id needs to
    # be sortable in a way that preserves the series order
    for series_id, mwidx in sorted(group_map.items()):
        if series_id[0] < 0:
            # skip our fake series with unwanted files
            continue
        mw = mwgroup[mwidx]
        if mw.image_shape is None:
            # this whole thing has now image data (maybe just PSg DICOMs)
            # nothing to see here, just move on
            continue
        dcminfo = mw.dcm_data
        series_files = [files[i] for i, s in enumerate(groups[0]) if s == series_id]
        # turn the series_id into a human-readable string -- string is needed
        # for JSON storage later on
        if per_studyUID:
            studyUID = series_id[2]
            series_id = series_id[:2]
        accession_number = dcminfo.get('AccessionNumber')

        series_id = '-'.join(map(str, series_id))

        size = list(mw.image_shape) + [len(series_files)]
        total += size[-1]
        if len(size) < 4:
            size.append(1)
        try:
            TR = float(dcminfo.RepetitionTime) / 1000.
        except AttributeError:
            TR = -1
        try:
            TE = float(dcminfo.EchoTime)
        except AttributeError:
            TE = -1
        try:
            refphys = str(dcminfo.ReferringPhysicianName)
        except AttributeError:
            refphys = '-'

        image_type = tuple(dcminfo.ImageType)
        motion_corrected = 'MoCo' in dcminfo.SeriesDescription \
                           or 'MOCO' in image_type

        if dcminfo.get([0x18,0x24], None):
            # GE and Philips scanners
            sequence_name = dcminfo[0x18,0x24].value
        elif dcminfo.get([0x19, 0x109c], None):
            # Siemens scanners
            sequence_name = dcminfo[0x19, 0x109c].value
        else:
            sequence_name = 'Not found'

        info = SeqInfo(
            total,
            os.path.split(series_files[0])[1],
            series_id,
            os.path.basename(os.path.dirname(series_files[0])),
            '-', '-',
            size[0], size[1], size[2], size[3],
            TR, TE,
            dcminfo.ProtocolName,
            motion_corrected,
            # New ones by us
            'derived' in [x.lower() for x in dcminfo.get('ImageType', [])],
            dcminfo.get('PatientID'),
            dcminfo.get('StudyDescription'),
            refphys,
            dcminfo.get('SeriesDescription'),
            sequence_name,
            image_type,
            accession_number,
            # For demographics to populate BIDS participants.tsv
            dcminfo.get('PatientAge'),
            dcminfo.get('PatientSex'),
            dcminfo.get('AcquisitionDate'),
        )
        # candidates
        # dcminfo.AccessionNumber
        #   len(dcminfo.ReferencedImageSequence)
        #   len(dcminfo.SourceImageSequence)
        # FOR demographics
        if per_studyUID:
            key = studyUID.split('.')[-1]
        elif per_accession_number:
            key = accession_number
        else:
            key = ''
        lgr.debug("%30s %30s %27s %27s %5s nref=%-2d nsrc=%-2d %s" % (
            key,
            info.series_id,
            dcminfo.SeriesDescription,
            dcminfo.ProtocolName,
            info.is_derived,
            len(dcminfo.get('ReferencedImageSequence', '')),
            len(dcminfo.get('SourceImageSequence', '')),
            info.image_type
        ))
        if per_studyUID:
            if studyUID not in seqinfo:
                seqinfo[studyUID] = ordereddict()
            seqinfo[studyUID][info] = series_files
        elif per_accession_number:
            if accession_number not in seqinfo:
                seqinfo[accession_number] = ordereddict()
            seqinfo[accession_number][info] = series_files
        else:
            seqinfo[info] = series_files

    if per_studyUID:
        lgr.info("Generated sequence info for %d studies with %d entries total",
                 len(seqinfo), sum(map(len, seqinfo.values())))
    elif per_accession_number:
        lgr.info("Generated sequence info for %d accession numbers with %d entries total",
                 len(seqinfo), sum(map(len, seqinfo.values())))
    else:
        lgr.info("Generated sequence info with %d entries", len(seqinfo))
    return seqinfo


def write_config(outfile, info):
    from pprint import PrettyPrinter
    with open(outfile, 'wt') as fp:
        fp.writelines(PrettyPrinter().pformat(info))


def read_config(infile):
    with open(infile, 'rt') as fp:
        info = eval(fp.read())
    return info


def conversion_info(subject, outdir, info, filegroup, ses=None):
    convert_info = []
    for key, items in info.items():
        if not items:
            continue
        template = key[0]
        outtype = key[1]
        # So no annotation_classes of any kind!  so if not used -- what was the
        # intension???? XXX
        outpath = outdir
        for idx, itemgroup in enumerate(items):
            if not isinstance(itemgroup, list):
                itemgroup = [itemgroup]
            for subindex, item in enumerate(itemgroup):
                parameters = {}
                if isinstance(item, dict):
                    parameters = {k: v for k, v in item.items()}
                    item = parameters['item']
                    del parameters['item']

                # some helper meta-varaibles
                parameters.update(dict(
                    item=idx + 1,
                    subject=subject,
                    seqitem=item,
                    subindex=subindex + 1,
                    session='ses-' + str(ses), # if not used -- not used -- not a problem
                    bids_subject_session_prefix=
                        'sub-%s' % subject + (('_ses-%s' % ses) if ses else ''),
                    bids_subject_session_dir=
                        'sub-%s' % subject + (('/ses-%s' % ses) if ses else ''),
                    # referring_physician_name
                    # study_description
                ))

                try:
                    files = filegroup[item]
                except KeyError:
                    files = filegroup[(str if PY3 else unicode)(item)]
                outprefix = template.format(**parameters)
                convert_info.append((os.path.join(outpath, outprefix), outtype, files))
    return convert_info


def embed_nifti(dcmfiles, niftifile, infofile, bids_info=None, force=False, min_meta=False):
    """

    If `niftifile` doesn't exist, it gets created out of the `dcmfiles` stack,
    and json representation of its meta_ext is returned (bug since should return
    both niftifile and infofile?)

    if `niftifile` exists, its affine's orientation information is used while
    establishing new `NiftiImage` out of dicom stack and together with `bids_info`
    (if provided) is dumped into json `infofile`

    Parameters
    ----------
    dcmfiles
    niftifile
    infofile
    bids_info
    force
    min_meta

    Returns
    -------
    niftifile, infofile

    """
    import nibabel as nb
    import os
    import json
    import re
    meta_info = {}
    if not min_meta:
        import dcmstack as ds
        stack = ds.parse_and_stack(dcmfiles, force=force).values()
        if len(stack) > 1:
            raise ValueError('Found multiple series')
        stack = stack[0]

        #Create the nifti image using the data array
        if not os.path.exists(niftifile):
            nifti_image = stack.to_nifti(embed_meta=True)
            nifti_image.to_filename(niftifile)
            return ds.NiftiWrapper(nifti_image).meta_ext.to_json()

        orig_nii = nb.load(niftifile)
        aff = orig_nii.get_affine()
        ornt = nb.orientations.io_orientation(aff)
        axcodes = nb.orientations.ornt2axcodes(ornt)
        new_nii = stack.to_nifti(voxel_order=''.join(axcodes), embed_meta=True)
        meta = ds.NiftiWrapper(new_nii).meta_ext.to_json()
        meta_info = json.loads(meta)
    if bids_info:
        if min_meta:
            meta_info = bids_info
        else:
            meta_info = dict(meta_info.items() + bids_info.items())
        try:
            task = re.search('(?<=_task-)\w+', os.path.basename(infofile)).group(0).split('_')[0]
            meta_info['TaskName'] = task
        except AttributeError: # not BIDS functional
            pass
    with open(infofile, 'wt') as fp:
        json.dump(meta_info, fp, indent=3, sort_keys=True)
    return niftifile, infofile


def compress_dicoms(dicom_list, out_prefix):
    """Archives DICOMs into a tarball

    Also tries to do it reproducibly, so takes the date for files
    and target tarball based on the series time (within the first file)

    Parameters
    ----------
    dicom_list : list of str
      list of dicom files
    out_prefix : str
      output path prefix, including the portion of the output file name
      before .dicom.tgz suffix

    Returns
    -------
    filename : str
      Result tarball
    """
    tmpdir = mkdtemp(prefix='dicomtar')
    outtar = out_prefix + '.dicom.tgz'
    if os.path.exists(outtar) and not global_options['overwrite']:
        raise RuntimeError("File %s already exists, will not override"
                           % outtar)
    # tarfile encodes current time.time inside making those non-reproducible
    # so we should choose which date to use.
    # Solution from DataLad although ugly enough:

    dicom_list = sorted(dicom_list)
    dcm_time = get_dicom_series_time(dicom_list)

    def _assign_dicom_time(ti):
        # Reset the date to match the one of the last commit, not from the
        # filesystem since git doesn't track those at all
        ti.mtime = dcm_time
        return ti

    # poor man mocking since can't rely on having mock
    try:
        import time
        _old_time = time.time
        time.time = lambda: dcm_time
        if os.path.lexists(outtar):
            os.unlink(outtar)
        with tarfile.open(outtar, 'w:gz', dereference=True) as tar:
            for filename in dicom_list:
                outfile = os.path.join(tmpdir, os.path.basename(filename))
                if not os.path.islink(outfile):
                    os.symlink(os.path.realpath(filename), outfile)
                # place into archive stripping any lead directories and
                # adding the one corresponding to prefix
                tar.add(outfile,
                        arcname=opj(basename(out_prefix),
                                    os.path.basename(outfile)),
                        recursive=False,
                        filter=_assign_dicom_time)
    finally:
        time.time = _old_time

    shutil.rmtree(tmpdir)
    return outtar


def get_dicom_series_time(dicom_list):
    """Get time in seconds since epoch from dicom series date and time

    Primarily to be used for reproducible time stamping
    """
    import time
    import calendar
    import dicom as dcm

    dcm = dcm.read_file(dicom_list[0], stop_before_pixels=True)
    dcm_date = dcm.SeriesDate  # YYYYMMDD
    dcm_time = dcm.SeriesTime  # HHMMSS.MICROSEC
    dicom_time_str = dcm_date + dcm_time.split('.', 1)[0]  # YYYYMMDDHHMMSS
    # convert to epoch
    return calendar.timegm(time.strptime(dicom_time_str, '%Y%m%d%H%M%S'))


def safe_copyfile(src, dest):
    """Copy file but blow if destination name already exists
    """
    if os.path.isdir(dest):
        dest = os.path.join(dest, os.path.basename(src))
    if os.path.lexists(dest):
        if not global_options['overwrite']:
            raise ValueError("was asked to copy %s but destination already exists: %s"
                             % (src, dest))
        else:
            # to make sure we can write there ... still fail if it is entire directory ;)
            os.unlink(dest)
    shutil.copyfile(src, dest)


def convert(items, symlink=True, converter=None,
        scaninfo_suffix='.json', custom_callable=None, with_prov=False,
        is_bids=False, sourcedir=None, outdir=None, min_meta=False):
    """Perform actual conversion (calls to converter etc) given info from
    heuristic's `infotodict`

    Parameters
    ----------
    items
    symlink
    converter
    scaninfo_suffix
    custom_callable
    with_prov
    is_bids
    sourcedir
    outdir
    min_meta

    Returns
    -------
    None
    """
    prov_files = []
    tmpdir = mkdtemp(prefix='heudiconvdcm')
    for item_idx, item in enumerate(items):
        prefix, outtypes, item_dicoms = item[:3]
        if not isinstance(outtypes, (list, tuple)):
            outtypes = [outtypes]

        prefix_dirname = os.path.dirname(prefix + '.ext')
        prov_file = None
        outname_bids = prefix + '.json'
        outname_bids_files = []  # actual bids files since dcm2niix might generate multiple ATM
        lgr.info('Converting %s (%d DICOMs) -> %s . '
                 'Converter: %s . Output types: %s',
                 prefix, len(item_dicoms), prefix_dirname, converter, outtypes)
        if not os.path.exists(prefix_dirname):
            os.makedirs(prefix_dirname)
        for outtype in outtypes:
            lgr.debug("Processing %d dicoms for output type %s",
                     len(item_dicoms), outtype)
            lgr.log(1, " those dicoms are: %s", item_dicoms)

            seqtype = basename(dirname(prefix)) if is_bids else None

            if outtype == 'dicom':
                if is_bids:
                    # mimic the same hierarchy location as the prefix
                    # although it could all have been done probably
                    # within heuristic really
                    sourcedir_ = os.path.join(
                        sourcedir,
                        os.path.dirname(
                            os.path.relpath(prefix, outdir)))
                    if not os.path.exists(sourcedir_):
                        os.makedirs(sourcedir_)
                    compress_dicoms(item_dicoms,
                                    opj(sourcedir_, os.path.basename(prefix)))
                else:
                    dicomdir = prefix + '_dicom'
                    if os.path.exists(dicomdir):
                        shutil.rmtree(dicomdir)
                    os.mkdir(dicomdir)
                    for filename in item_dicoms:
                        outfile = os.path.join(dicomdir, os.path.split(filename)[1])
                        if not os.path.islink(outfile):
                            if symlink:
                                os.symlink(filename, outfile)
                            else:
                                os.link(filename, outfile)
            elif outtype in ['nii', 'nii.gz']:
                outname = prefix + '.' + outtype
                scaninfo = prefix + scaninfo_suffix
                if not os.path.exists(outname):
                    if with_prov:
                        from nipype import config
                        config.enable_provenance()
                    from nipype import Function, Node
                    from nipype.interfaces.base import isdefined
                    if converter == 'dcm2niix':
                        from nipype.interfaces.dcm2nii import Dcm2nii, Dcm2niix
                        convertnode = Node(Dcm2niix(), name='convert')
                        convertnode.base_dir = tmpdir
                        # need to be abspaths!
                        item_dicoms = list(map(os.path.abspath, item_dicoms))
                        convertnode.inputs.source_names = item_dicoms
                        if converter == 'dcm2nii':
                            convertnode.inputs.gzip_output = outtype == 'nii.gz'
                        else:
                            if not is_bids:
                                convertnode.inputs.bids_format = False
                            convertnode.inputs.out_filename = os.path.basename(prefix_dirname)
                        convertnode.inputs.terminal_output = 'allatonce'
                        res = convertnode.run()

                        if isdefined(res.outputs.bvecs):
                            outname_bvecs = prefix + '.bvec'
                            outname_bvals = prefix + '.bval'
                            safe_copyfile(res.outputs.bvecs, outname_bvecs)
                            safe_copyfile(res.outputs.bvals, outname_bvals)

                        res_files = res.outputs.converted_files
                        if isinstance(res_files, list):
                            # TODO: move into a function
                            # by default just suffix them up
                            suffixes = None
                            # we should provide specific handling for fmap,
                            # dwi etc which might spit out multiple files
                            if is_bids:
                                if seqtype == 'fmap':
                                    # expected!
                                    suffixes = ["%d" % (i+1) for i in range(len(res_files))]
                            if not suffixes:
                                lgr.warning(
                                    "Following series files likely have "
                                    "multiple (%d) volumes (orientations?) "
                                    "generated: %s ...",
                                    len(res_files), item_dicoms[0]
                                )
                                suffixes = ['-%d' % (i+1) for i in range(len(res_files))]

                            # Also copy BIDS files although they might need to be merged/postprocessed later
                            if converter == 'dcm2niix' and isdefined(res.outputs.bids):
                                assert(len(res.outputs.bids) == len(res_files))
                                bids_files = res.outputs.bids
                            else:
                                bids_files = [None] * len(res_files)

                            for fl, suffix, bids_file in zip(res_files, suffixes, bids_files):
                                outname = "%s%s.%s" % (prefix, suffix, outtype)
                                safe_copyfile(fl, outname)
                                if bids_file:
                                    outname_bids_file = "%s%s.json" % (prefix, suffix)
                                    safe_copyfile(bids_file, outname_bids_file)
                                    outname_bids_files.append(outname_bids_file)

                        else:
                            safe_copyfile(res_files, outname)
                            if converter == 'dcm2niix' and isdefined(res.outputs.bids):
                                try:
                                    safe_copyfile(res.outputs.bids, outname_bids)
                                    outname_bids_files.append(outname_bids)
                                except TypeError as exc:  ##catch lists
                                    lgr.warning(
                                        "There was someone catching lists!: %s", exc
                                    )
                                    continue

                        # save acquisition time information if it's BIDS
                        # at this point we still have acquisition date
                        if is_bids:
                            save_scans_key(item, outname_bids_files)
                        # Fix up and unify BIDS files
                        tuneup_bids_json_files(outname_bids_files)
                        # we should provide specific handling for fmap,
                        # dwi etc .json of which should get merged to satisfy
                        # BIDS.  BUT wer might be somewhat not in time for a
                        # party here since we sorted into multiple seqinfo
                        # (e.g. magnitude, phase for fmap so we might want
                        # to sort them into a single one)

                    if with_prov:
                        prov_file = prefix + '_prov.ttl'
                        safe_copyfile(os.path.join(convertnode.base_dir,
                                                     convertnode.name,
                                                    'provenance.ttl'),
                                        prov_file)
                        prov_files.append(prov_file)

                if len(outname_bids_files) > 1:
                    lgr.warning(
                        "For now not embedding BIDS and info generated .nii.gz itself since sequence produced multiple files")
                else:
                    #if not is_bids or converter != 'dcm2niix': ##uses dcm2niix's infofile
                    embed_metadata_from_dicoms(converter, is_bids, item_dicoms,
                                               outname, outname_bids, prov_file,
                                               scaninfo, tmpdir, with_prov,
                                               min_meta)
                if exists(scaninfo):
                    lgr.info("Post-treating %s file", scaninfo)
                    treat_infofile(scaninfo)
                os.chmod(outname, 0o0440)

        if custom_callable is not None:
            custom_callable(*item)
    shutil.rmtree(tmpdir)


def get_formatted_scans_key_row(item):
    """
    Parameters
    ----------
    item

    Returns
    -------
    row: list
        [ISO acquisition time, performing physician name, random string]

    """
    dcm_fn = item[-1][0]
    mw = ds.wrapper_from_data(dcm.read_file(dcm_fn, stop_before_pixels=True))
    # we need to store filenames and acquisition times
    # parse date and time and get it into isoformat
    date = mw.dcm_data.ContentDate
    time = mw.dcm_data.ContentTime.split('.')[0]
    td = time + date
    acq_time = datetime.strptime(td, '%H%M%S%Y%m%d').isoformat()
    # add random string
    randstr = ''.join(map(chr, sample(k=8, population=range(33, 127))))
    row = [acq_time, mw.dcm_data.PerformingPhysicianName, randstr]
    # empty entries should be 'n/a'
    # https://github.com/dartmouth-pbs/heudiconv/issues/32
    row = ['n/a' if not str(e) else e for e in row]
    return row


def add_rows_to_scans_keys_file(fn, newrows):
    """
    Add new rows to file fn for scans key filename

    Parameters
    ----------
    fn: filename
    newrows: extra rows to add
        dict fn: [acquisition time, referring physician, random string]
    """
    if lexists(fn):
        with open(fn, 'r') as csvfile:
            reader = csv.reader(csvfile, delimiter='\t')
            existing_rows = [row for row in reader]
        # skip header
        fnames2info = {row[0]: row[1:] for row in existing_rows[1:]}

        newrows_key = newrows.keys()
        newrows_toadd = list(set(newrows_key) - set(fnames2info.keys()))
        for key_toadd in newrows_toadd:
            fnames2info[key_toadd] = newrows[key_toadd]
        # remove
        os.unlink(fn)
    else:
        fnames2info = newrows

    header = ['filename', 'acq_time', 'operator', 'randstr']
    # save
    with open(fn, 'a') as csvfile:
        writer = csv.writer(csvfile, delimiter='\t')
        writer.writerow(header)
        for key in sorted(fnames2info.keys()):
            writer.writerow([key] + fnames2info[key])


def _find_subj_ses(f_name):
    """Given a path to the bids formatted filename parse out subject/session"""
    # we will allow the match at either directories or within filename
    # assuming that bids layout is "correct"
    regex = re.compile('sub-(?P<subj>[a-zA-Z0-9]*)([/_]ses-(?P<ses>[a-zA-Z0-9]*))?')
    res = regex.search(f_name).groupdict()
    return res.get('subj'), res.get('ses', None)


def save_scans_key(item, bids_files):
    """
    Parameters
    ----------
    items:
    bids_files: str or list

    Returns
    -------

    """
    rows = dict()
    assert bids_files, "we do expect some files since it was called"
    # we will need to deduce subject and session from the bids_filename
    # and if there is a conflict, we would just blow since this function
    # should be invoked only on a result of a single item conversion as far
    # as I see it, so should have the same subject/session
    subj, ses = None, None
    for bids_file in bids_files:
        # get filenames
        f_name = '/'.join(bids_file.split('/')[-2:])
        f_name = f_name.replace('json', 'nii.gz')
        rows[f_name] = get_formatted_scans_key_row(item)
        subj_, ses_ = _find_subj_ses(f_name)
        if subj and subj_ != subj:
            raise ValueError(
                "We found before subject %s but now deduced %s from %s"
                % (subj, subj_, f_name)
            )
        subj = subj_
        if ses and ses_ != ses:
            raise ValueError(
                "We found before session %s but now deduced %s from %s"
                % (ses, ses_, f_name)
            )
        ses = ses_
    # where should we store it?
    output_dir = dirname(dirname(bids_file))
    # save
    ses = '_ses-%s' % ses if ses else ''
    add_rows_to_scans_keys_file(
        pjoin(output_dir, 'sub-{0}{1}_scans.tsv'.format(subj, ses)),
        rows
    )


def tuneup_bids_json_files(json_files):
    """Given a list of BIDS .json files, e.g. """
    if not json_files:
        return

    # Harmonize generic .json formatting
    for jsonfile in json_files:
        json_ = json.load(open(jsonfile))
        # sanitize!
        for f1 in ['Acquisition', 'Study', 'Series']:
            for f2 in ['DateTime', 'Date']:
                json_.pop(f1 + f2, None)
        # TODO:  should actually be placed into series file which must
        #        go under annex (not under git) and marked as sensitive
        if 'Date' in str(json_):
            # Let's hope no word 'Date' comes within a study name or smth like
            # that
            raise ValueError("There must be no dates in .json sidecar")
        json.dump(json_, open(jsonfile, 'w'), indent=2)

    # Load the beast
    seqtype = basename(dirname(jsonfile))

    if seqtype == 'fmap':
        json_basename = '_'.join(jsonfile.split('_')[:-1])
        # if we got by now all needed .json files -- we can fix them up
        # unfortunately order of "items" is not guaranteed atm
        if len(glob(json_basename + '*.json')) == 3:
            json_phasediffname = json_basename + '_phasediff.json'
            json_ = json.load(open(json_phasediffname))
            # TODO: we might want to reorder them since ATM
            # the one for shorter TE is the 2nd one!
            # For now just save truthfully by loading magnitude files
            lgr.debug("Placing EchoTime fields into phasediff file")
            for i in 1, 2:
                try:
                    json_['EchoTime%d' % i] = \
                        json.load(open(json_basename + '_magnitude%d.json' % i))[
                            'EchoTime']
                except IOError as exc:
                    lgr.error("Failed to open magnitude file: %s", exc)

            # might have been made R/O already
            os.chmod(json_phasediffname, 0o0664)
            json.dump(json_, open(json_phasediffname, 'w'), indent=2)
            os.chmod(json_phasediffname, 0o0444)

        # phasediff one should contain two PhaseDiff's
        #  -- one for original amplitude and the other already replicating what is there
        # so let's load json files for magnitudes and
        # place them into phasediff


def embed_metadata_from_dicoms(converter, is_bids, item_dicoms, outname,
                               outname_bids, prov_file, scaninfo, tmpdir,
                               with_prov, min_meta):
    """
    Enhance sidecar information file with more information from DICOMs

    Parameters
    ----------
    converter
    is_bids
    item_dicoms
    outname
    outname_bids
    prov_file
    scaninfo
    tmpdir
    with_prov
    min_meta

    Returns
    -------

    """
    from nipype import Node, Function
    embedfunc = Node(Function(input_names=['dcmfiles',
                                           'niftifile',
                                           'infofile',
                                           'bids_info',
                                           'force',
                                           'min_meta'],
                              output_names=['outfile',
                                            'meta'],
                              function=embed_nifti),
                     name='embedder')
    embedfunc.inputs.dcmfiles = item_dicoms
    embedfunc.inputs.niftifile = os.path.abspath(outname)
    embedfunc.inputs.infofile = os.path.abspath(scaninfo)
    embedfunc.inputs.min_meta = min_meta
    if is_bids and (converter == 'dcm2niix'):
        embedfunc.inputs.bids_info = load_json(os.path.abspath(outname_bids))
    else:
        embedfunc.inputs.bids_info = None
    embedfunc.inputs.force = True
    embedfunc.base_dir = tmpdir
    cwd = os.getcwd()
    try:
        """
        Ran into
INFO: Executing node embedder in dir: /tmp/heudiconvdcm2W3UQ7/embedder
ERROR: Embedding failed: [Errno 13] Permission denied: '/inbox/BIDS/tmp/test2-jessie/Wheatley/Beau/1007_personality/sub-sid000138/fmap/sub-sid000138_3mm_run-01_phasediff.json'
while
HEUDICONV_LOGLEVEL=WARNING time bin/heudiconv -f heuristics/dbic_bids.py -c dcm2niix -o /inbox/BIDS/tmp/test2-jessie --bids --datalad /inbox/DICOM/2017/01/28/A000203

so it seems that there is a filename collision so it tries to save into the same file name
and there was a screw up for that A

/mnt/btrfs/dbic/inbox/DICOM/2017/01/28/A000203
        StudySessionInfo(locator='Wheatley/Beau/1007_personality', session=None, subject='sid000138') 16 sequences
        StudySessionInfo(locator='Wheatley/Beau/1007_personality', session=None, subject='a000203') 2 sequences


in that one though
        """
        if global_options['overwrite'] and os.path.lexists(scaninfo):
            # TODO: handle annexed file case
            if not os.path.islink(scaninfo):
                os.chmod(scaninfo, 0o0660)
        res = embedfunc.run()
        os.chmod(scaninfo, 0o0444)
        if with_prov:
            g = res.provenance.rdf()
            g.parse(prov_file,
                    format='turtle')
            g.serialize(prov_file, format='turtle')
            os.chmod(prov_file, 0o0440)
    except Exception as exc:
        lgr.error("Embedding failed: %s", str(exc))
        os.chdir(cwd)


def treat_infofile(filename):
    """Tune up generated .json file (slim down, pretty-print for humans).

    Was difficult to do within embed_nifti since it has no access to our functions
    """
    with open(filename) as f:
        j = json.load(f)

    j_slim = slim_down_info(j)
    j_pretty = json_dumps_pretty(j_slim, indent=2, sort_keys=True)

    os.chmod(filename, 0o0664)
    with open(filename, 'wt') as fp:
        fp.write(j_pretty)
    os.chmod(filename, 0o0444)


def convert_dicoms(sid,
                   dicoms,
                   outdir,
                   heuristic,
                   converter,
                   anon_sid=None, anon_outdir=None,
                   with_prov=False,
                   ses=None,
                   is_bids=False,
                   seqinfo=None,
                   min_meta=False):
    if dicoms:
        lgr.info("Processing %d dicoms", len(dicoms))
    elif seqinfo:
        lgr.info("Processing %d pre-sorted seqinfo entries", len(seqinfo))
    else:
        raise ValueError("neither dicoms nor seqinfo dict was provided")

    # in this reimplementation we can have only a single session assigned
    # at this point
    # dcmsessions =

    if is_bids and not sid.isalnum(): # alphanumeric only
        old_sid = sid
        cleaner = lambda y: ''.join([x for x in y if x.isalnum()])
        sid = cleaner(sid) #strip out
        lgr.warning('{0} contained nonalphanumeric character(s), subject '
                 'ID was cleaned to be {1}'.format(old_sid, sid))

    #
    # Annonimization parameters
    #
    if anon_sid is None:
        anon_sid = sid
    if anon_outdir is None:
        anon_outdir = outdir

    # Figure out where to stick supplemental info dicoms
    idir = os.path.join(outdir, '.heudiconv', sid)
    # THAT IS WHERE WE MUST KNOW ABOUT SESSION ALREADY!
    if is_bids and ses:
        idir = os.path.join(idir, 'ses-%s' % str(ses))
    # yoh: in my case if idir exists, it means that that study/subject/session
    # is already processed
    if anon_outdir == outdir:
        # if all goes into a single dir, have a dedicated 'info' subdir
        idir = os.path.join(idir, 'info')
    if not os.path.exists(idir):
        os.makedirs(idir)

    shutil.copy(heuristic.filename, idir)
    ses_suffix = "_ses-%s" % ses if ses is not None else ""
    info_file = os.path.join(idir, '%s%s.auto.txt' % (sid, ses_suffix))
    edit_file = os.path.join(idir, '%s%s.edit.txt' % (sid, ses_suffix))
    filegroup_file = os.path.join(idir, 'filegroup%s.json' % ses_suffix)

    if os.path.exists(edit_file):  # XXX may be condition on seqinfo is None
        lgr.info("Reloading existing filegroup.json because %s exists",
                 edit_file)
        info = read_config(edit_file)
        filegroup = load_json(filegroup_file)
        # XXX Yarik finally understood why basedir was dragged along!
        # So we could reuse the same PATHs definitions possibly consistent
        # across re-runs... BUT that wouldn't work anyways if e.g.
        # DICOMs dumped with SOP UUIDs thus differing across runs etc
        # So either it would need to be brought back or reconsidered altogether
        # (since no sample data to test on etc)
    else:
        # TODO -- might have been done outside already!
        if dicoms:
            seqinfo = group_dicoms_into_seqinfos(
                dicoms,
                file_filter=getattr(heuristic, 'filter_files', None),
                dcmfilter=getattr(heuristic, 'filter_dicom', None),
                grouping=None,  # no groupping
            )
        seqinfo_list = list(seqinfo.keys())

        filegroup = {si.series_id: x for si, x in seqinfo.items()}

        save_json(filegroup_file, filegroup)
        dicominfo_file = os.path.join(idir, 'dicominfo%s.tsv' % ses_suffix)
        with open(dicominfo_file, 'wt') as fp:
            for seq in seqinfo_list:
                fp.write('\t'.join([str(val) for val in seq]) + '\n')
        lgr.debug("Calling out to %s.infodict", heuristic)
        info = heuristic.infotodict(seqinfo_list)
        write_config(info_file, info)
        write_config(edit_file, info)

    #
    # Conversion
    #

    sourcedir = None
    if is_bids:
        sourcedir = os.path.join(outdir, 'sourcedata')
        # the other portion of the path would mimic BIDS layout
        # so we don't need to worry here about sub, ses at all
        tdir = anon_outdir
    else:
        tdir = os.path.join(anon_outdir, anon_sid)

    if converter != 'none':
        lgr.info("Doing conversion using %s", converter)
        cinfo = conversion_info(anon_sid, tdir, info, filegroup,
                                ses=ses)
        convert(cinfo,
                converter=converter,
                scaninfo_suffix=getattr(
                    heuristic, 'scaninfo_suffix', '.json'),
                custom_callable=getattr(
                    heuristic, 'custom_callable', None),
                with_prov=with_prov,
                is_bids=is_bids,
                sourcedir=sourcedir,
                outdir=tdir)

    if is_bids:
        if seqinfo:
            keys = list(seqinfo)
            add_participant_record(
                anon_outdir,
                anon_sid,
                keys[0].patient_age,
                keys[0].patient_sex,
            )
        populate_bids_templates(
            anon_outdir,
            getattr(heuristic, 'DEFAULT_FIELDS', {})
        )


def get_annonimized_sid(sid, anon_sid_cmd):
    anon_sid = sid
    if anon_sid_cmd is not None:
        from subprocess import check_output
        anon_sid = check_output([anon_sid_cmd, sid]).strip()
        lgr.info("Annonimized sid %s into %s", sid, anon_sid)
    return anon_sid


def get_extracted_dicoms(fl):
    """Given a list of files, possibly extract some from tarballs

    For 'classical' heudiconv, if multiple tarballs are provided, they correspond
    to different sessions, so here we would group into sessions and return
    pairs  `sessionid`, `files`  with `sessionid` being None if no "sessions"
    detected for that file or there was just a single tarball in the list
    """
    # TODO: bring check back?
    # if any(not tarfile.is_tarfile(i) for i in fl):
    #     raise ValueError("some but not all input files are tar files")

    # tarfiles already know what they contain, and often the filenames
    # are unique, or at least in a unqiue subdir per session
    # strategy: extract everything in a temp dir and assemble a list
    # of all files in all tarballs
    tmpdir = tempdirs(prefix='heudiconvdcm')

    sessions = defaultdict(list)
    session = 0
    if not isinstance(fl, (list, tuple)):
        fl = list(fl)

    # needs sorting to keep the generated "session" label deterministic
    for i, t in enumerate(sorted(fl)):
        # "classical" heudiconv has that heuristic to handle multiple
        # tarballs as providing different sessions per each tarball
        if not tarfile.is_tarfile(t):
            sessions[None].append(t)
            continue  # the rest is tarball specific

        tf = tarfile.open(t)
        # check content and sanitize permission bits
        tmembers = tf.getmembers()
        for tm in tmembers:
            tm.mode = 0o700
        # get all files, assemble full path in tmp dir
        tf_content = [m.name for m in tmembers if m.isfile()]
        # store full paths to each file, so we don't need to drag along
        # tmpdir as some basedir
        sessions[session] = [opj(tmpdir, f) for f in tf_content]
        session += 1
        # extract into tmp dir
        tf.extractall(path=tmpdir, members=tmembers)

    if session == 1:
        # we had only 1 session, so no really multiple sessions according
        # to classical 'heudiconv' assumptions, thus just move them all into
        # None
        sessions[None] += sessions.pop(0)

    return sessions.items()


def load_heuristic(heuristic_file):
    """Load heuristic from the file, return the module
    """
    path, fname = os.path.split(heuristic_file)
    sys.path.append(path)
    mod = __import__(fname.split('.')[0])
    mod.filename = heuristic_file
    return mod


def get_study_sessions(dicom_dir_template, files_opt, heuristic, outdir,
                       session, sids, grouping='studyUID'):
    """Given options from cmdline sort files or dicom seqinfos into
    study_sessions which put together files for a single session of a subject
    in a study

    Two major possible workflows:
    - if dicom_dir_template provided -- doesn't pre-load DICOMs and just
      loads files pointed by each subject and possibly sessions as corresponding
      to different tarballs
    - if files_opt is provided, sorts all DICOMs it can find under those paths
    """
    study_sessions = {}
    if dicom_dir_template:
        dicom_dir_template = os.path.abspath(dicom_dir_template)
        assert not files_opt  # see above TODO
        assert sids
        # expand the input template
        if '{subject}' not in dicom_dir_template:
            raise ValueError(
                "dicom dir template must have {subject} as a placeholder for a "
                "subject id.  Got %r" % dicom_dir_template)
        for sid in sids:
            sdir = dicom_dir_template.format(subject=sid, session=session)
            # and see what matches
            files = sorted(glob(sdir))
            for session_, files_ in get_extracted_dicoms(files):
                if session_ is not None and session:
                    lgr.warning(
                        "We had session specified (%s) but while analyzing "
                        "files got a new value %r (using it instead)"
                        % (session, session_))
                # in this setup we do not care about tracking "studies" so
                # locator would be the same None
                study_sessions[
                    StudySessionInfo(
                        None,
                        session_ if session_ is not None else session,
                        sid,
                    )] = files_
    else:
        # prep files
        assert files_opt
        assert not sids
        files = []
        for f in files_opt:
            if isdir(f):
                files += sorted(find_files(
                    '.*', topdir=f, exclude_vcs=True, exclude="/\.datalad/"))
            else:
                files.append(f)

        # in this scenario we don't care about sessions obtained this way
        files_ = []
        for _, files_ex in get_extracted_dicoms(files):
            files_ += files_ex

        # sort all DICOMS using heuristic
        # TODO:  this one is not groupping by StudyUID but may be we should!
        seqinfo_dict = group_dicoms_into_seqinfos(
            files_,
            file_filter=getattr(heuristic, 'filter_files', None),
            dcmfilter=getattr(heuristic, 'filter_dicom', None),
            grouping=grouping)

        if not getattr(heuristic, 'infotoids', None):
            raise NotImplementedError(
                "For now, if no subj template is provided, requiring "
                "heuristic to have infotoids")

        for studyUID, seqinfo in seqinfo_dict.items():
            # so we have a single study, we need to figure out its
            # locator, session, subject
            # TODO: Try except to ignore those we can't handle?
            # actually probably there should be a dedicated exception for
            # heuristics to throw if they detect that the study they are given
            # is not the one they would be willing to work on
            ids = heuristic.infotoids(seqinfo.keys(), outdir=outdir)
            # TODO:  probably infotoids is doomed to do more and possibly
            # split into multiple sessions!!!! but then it should be provided
            # full seqinfo with files which it would place into multiple groups
            lgr.info("Study session for %s" % str(ids))
            study_session_info = StudySessionInfo(
                ids.get('locator'),
                ids.get('session', session) or session,
                ids.get('subject', None))
            if study_session_info in study_sessions:
                #raise ValueError(
                lgr.warning(
                    "We already have a study session with the same value %s"
                    % repr(study_session_info))
                continue # skip for now
            study_sessions[study_session_info] = seqinfo

    return study_sessions

#
# Additional handlers
#
def is_interactive():
    """Return True if all in/outs are tty"""
    # TODO: check on windows if hasattr check would work correctly and add value:
    #
    return sys.stdin.isatty() and sys.stdout.isatty() and sys.stderr.isatty()


def create_file_if_missing(filename, content):
    """Create file if missing, so we do not override any possibly introduced changes"""
    if exists(filename):
        return False
    with open(filename, 'w') as f:
        f.write(content)
    return True


def populate_bids_templates(path, defaults={}):
    # dataset descriptor
    lgr.info("Populating template files under %s", path)
    descriptor = opj(path, 'dataset_description.json')
    if not exists(descriptor):
        save_json(descriptor,
              ordereddict([
                  ('Name', "TODO: name of the dataset"),
                  ('BIDSVersion', "1.0.1"),
                  ('License', defaults.get('License', "TODO: choose a license, e.g. PDDL (http://opendatacommons.org/licenses/pddl/)")),
                  ('Authors', defaults.get('Authors', ["TODO:", "First1 Last1", "First2 Last2", "..."])),
                  ('Acknowledgements', defaults.get('Acknowledgements', 'TODO: whom you want to acknowledge')),
                  ('HowToAcknowledge', "TODO: describe how to acknowledge -- either cite a corresponding paper, or just in acknowledgement section"),
                  ('Funding', ["TODO", "GRANT #1", "GRANT #2"]),
                  ('ReferencesAndLinks', ["TODO", "List of papers or websites"]),
                  ('DatasetDOI', 'TODO: eventually a DOI for the dataset')
        ]))

    sourcedata_README = opj(path, 'sourcedata', 'README')
    if exists(dirname(sourcedata_README)):
        create_file_if_missing(
            sourcedata_README,
            """\
TODO: Provide description about source data, e.g.

Directory below contains DICOMS compressed into tarballs per each sequence,
replicating directory hierarchy of the BIDS dataset itself.
""")

    create_file_if_missing(
        opj(path, 'CHANGES'),
        """\
0.0.1  Initial data acquired

TODOs:
  - verify and possibly extend information in participants.tsv
    (see for example http://datasets.datalad.org/?dir=/openfmri/ds000208)
  - fill out dataset_description.json, README, sourcedata/README (if present)
  - provide _events.tsv file for each _bold.nii.gz with onsets of events
    (see  "8.5 Task events"  of BIDS specification)
""")

    create_file_if_missing(
        opj(path, 'README'),
        """\
TODO: Provide description for the dataset -- basic details about the study,
possibly pointing to pre-registration (if public or embargoed)
""")

    # TODO: collect all task- .json files for func files to
    tasks = {}
    # way too many -- let's just collect all which are the same!
    # FIELDS_TO_TRACK = {'RepetitionTime', 'FlipAngle', 'EchoTime', 'Manufacturer', 'SliceTiming', ''}
    for fpath in find_files('.*_task-.*\_bold\.json', topdir=path,
                        exclude_vcs=True, exclude="/\.(datalad|heudiconv)/"):
        task = re.sub('.*_(task-[^_\.]*(_acq-[^_\.]*)?)_.*', r'\1', fpath)
        j = load_json(fpath)
        if task not in tasks:
            tasks[task] = j
        else:
            rec = tasks[task]
            # let's retain only those fields which have the same value
            for field in sorted(rec):
                if field not in j or j[field] != rec[field]:
                    del rec[field]

        # create a stub onsets file for each one of those
        suf = '_bold.json'
        assert fpath.endswith(suf)
        events_file = fpath[:-len(suf)] + '_events.tsv'
        lgr.debug("Generating %s", events_file)
        with open(events_file, 'w') as f:
            f.write("onset\tduration\ttrial_type\tTODO -- fill in rows and add more tab-separated columns if desired")

    # - extract tasks files stubs
    for task_acq, fields in tasks.items():
        task_file = opj(path, task_acq + '_bold.json')
        lgr.debug("Generating %s", task_file)
        fields["TaskName"] = "TODO: full task name for %s" % task_acq.split('_')[0].split('-')[1]
        fields["CogAtlasID"] = "TODO"
        with open(task_file, 'w') as f:
            f.write(json_dumps_pretty(fields, indent=2, sort_keys=True))


def add_participant_record(studydir, subject, age, sex):
    participants_tsv = opj(studydir, 'participants.tsv')
    participant_id = 'sub-%s' % subject

    if not create_file_if_missing(
        participants_tsv,
        '\t'.join(['participant_id', 'age', 'sex', 'group']) + '\n'
    ):
        # check if may be subject record already exists
        with open(participants_tsv) as f:
            f.readline()
            known_subjects = {l.split('\t')[0] for l in f.readlines()}
        if participant_id in known_subjects:
            # already there -- not adding
            return

    # Add a new participant
    with open(participants_tsv, 'a') as f:
        f.write('\t'.join(map(str, [
            participant_id,
            age.lstrip('0').rstrip('Y') if age else 'N/A',
            sex,
            'control']))
        + '\n')


def mark_sensitive(ds, path_glob=None):
    """

    Parameters
    ----------
    ds : Dataset to operate on
    path_glob : str, optional
      glob of the paths within dataset to work on

    Returns
    -------
    None
    """
    sens_kwargs = dict(
        init=[('distribution-restrictions', 'sensitive')]
    )
    if path_glob:
        paths = glob(opj(ds.path, path_glob))
        if not paths:
            return
        sens_kwargs['path'] = paths
    ds.metadata(**sens_kwargs)


def add_to_datalad(topdir, studydir, msg=None, bids=False):
    """Do all necessary preparations (if were not done before) and save
    """
    from datalad.api import create
    from datalad.api import Dataset
    from datalad.support.annexrepo import AnnexRepo

    from datalad.support.external_versions import external_versions
    assert external_versions['datalad'] >= '0.5.1', "Need datalad >= 0.5.1"

    studyrelpath = os.path.relpath(studydir, topdir)
    assert not studyrelpath.startswith(os.path.pardir)  # so we are under
    # now we need to test and initiate a DataLad dataset all along the path
    curdir_ = topdir
    superds = None
    subdirs = [''] + studyrelpath.split(os.path.sep)
    for isubdir, subdir in enumerate(subdirs):
        curdir_ = opj(curdir_, subdir)
        ds = Dataset(curdir_)
        if not ds.is_installed():
            lgr.info("Initiating %s", ds)
            # would require annex > 20161018 for correct operation on annex v6
            ds_ = create(curdir_, dataset=superds,
                         force=True,
                         no_annex=True,  # need to add .gitattributes first anyways
                         shared_access='all',
                         annex_version=6)
            assert ds == ds_
        assert ds.is_installed()
        superds = ds

    create_file_if_missing(
        opj(studydir, '.gitattributes'),
        """\
* annex.largefiles=(largerthan=100kb)
*.json annex.largefiles=nothing
*.txt annex.largefiles=nothing
*.tsv annex.largefiles=nothing
*.nii.gz annex.largefiles=anything
*.tgz annex.largefiles=anything
*_scans.tsv annex.largefiles=anything
""")
    # so for mortals it just looks like a regular directory!
    if not ds.config.get('annex.thin'):
        ds.config.add('annex.thin', 'true', where='local')
    # initialize annex there if not yet initialized
    AnnexRepo(ds.path, init=True)
    # ds might have memories of having ds.repo GitRepo
    superds = None
    del ds
    ds = Dataset(studydir)
    # Add doesn't have all the options of save such as msg and supers
    ds.add('.gitattributes', to_git=True, save=False)
    dsh = None
    if os.path.lexists(os.path.join(ds.path, '.heudiconv')):
        dsh = Dataset(opj(ds.path, '.heudiconv'))
        if not dsh.is_installed():
            # we need to create it first
            dsh = ds.create(path='.heudiconv',
                            force=True,
                            shared_access='all')
        # Since .heudiconv could contain sensitive information
        # we place all files under annex and then add
        if create_file_if_missing(
            opj(dsh.path, '.gitattributes'),
            """* annex.largefiles=anything
            """):
            dsh.add('.gitattributes', message="Added gitattributes to place all content under annex")
    ds.add('.', recursive=True, save=False,
           # not in effect! ?
           #annex_add_opts=['--include-dotfiles']
           )

    # TODO: filter for only changed files?
    # Provide metadata for sensitive information
    mark_sensitive(ds, 'sourcedata')
    mark_sensitive(ds, '*_scans.tsv')  # top level
    mark_sensitive(ds, '*/*_scans.tsv')  # within subj
    mark_sensitive(ds, '*/*/*_scans.tsv')  # within sess/subj
    mark_sensitive(ds, '*/anat')  # within subj
    mark_sensitive(ds, '*/*/anat')  # within ses/subj
    if dsh:
        mark_sensitive(dsh)  # entire .heudiconv!
        dsh.save(message=msg)
    ds.save(message=msg, recursive=True, super_datasets=True)

    assert not ds.repo.dirty
    # TODO:  they are still appearing as native annex symlinked beasts
    """
    TODOs:
    it needs
    - unlock  (thin will be in effect)
    - save/commit (does modechange 120000 => 100644

    - could potentially somehow automate that all:
      http://git-annex.branchable.com/tips/automatically_adding_metadata/
    - possibly even make separate sub-datasets for originaldata, derivatives ?
    """


_sys_excepthook = sys.excepthook  # Just in case we ever need original one


def setup_exceptionhook():
    """Overloads default sys.excepthook with our exceptionhook handler.

       If interactive, our exceptionhook handler will invoke
       pdb.post_mortem; if not interactive, then invokes default handler.
    """

    def _pdb_excepthook(type, value, tb):
        if is_interactive():
            import traceback
            import pdb
            traceback.print_exception(type, value, tb)
            print()
            pdb.post_mortem(tb)
        else:
            lgr.warn("We cannot setup exception hook since not in interactive mode")
            _sys_excepthook(type, value, tb)

    sys.excepthook = _pdb_excepthook


def _main(args):
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
