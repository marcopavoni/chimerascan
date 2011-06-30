#!/usr/bin/env python
'''
Created on Jan 5, 2011

@author: mkiyer

chimerascan: chimeric transcript discovery using RNA-seq

Copyright (C) 2011 Matthew Iyer

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
'''
__author__ = "Matthew Iyer"
__copyright__ = "Copyright 2011, chimerascan project"
__credits__ = ["Matthew Iyer", "Christopher Maher"]
__license__ = "GPL"
__version__ = "0.4.0"
__maintainer__ = "Matthew Iyer"
__email__ = "mkiyer@med.umich.edu"
__status__ = "beta"

import logging
import os
import subprocess
import sys
import shutil
from optparse import OptionParser, OptionGroup
import xml.etree.ElementTree as etree 

# check for python version 2.6.0 or greater
if sys.version_info < (2,6,0):
    sys.stderr.write("You need python 2.6 or later to run chimerascan\n")
    sys.exit(1)

# local imports
import chimerascan.pysam as pysam
import chimerascan.lib.config as config
from chimerascan.lib.config import JOB_SUCCESS, JOB_ERROR, MIN_SEGMENT_LENGTH, indent
from chimerascan.lib.base import LibraryTypes, check_executable, \
    get_read_length, parse_library_type, parse_bool
from chimerascan.lib.seq import FASTQ_QUAL_FORMATS, SANGER_FORMAT

from chimerascan.pipeline.align_bowtie import align_pe, align_sr, trim_align_pe_sr
from chimerascan.pipeline.profile_insert_size import InsertSizeDistribution
from chimerascan.pipeline.find_discordant_reads import find_discordant_fragments
from chimerascan.pipeline.nominate_chimeras import nominate_chimeras
from chimerascan.pipeline.chimeras_to_breakpoints import determine_chimera_breakpoints
from chimerascan.pipeline.nominate_spanning_reads import nominate_spanning_reads


#from chimerascan.pipeline.align_full import align_pe_full, align_sr_full
#from chimerascan.pipeline.align_segments import align, determine_read_segments
#from chimerascan.pipeline.merge_read_pairs import merge_read_pairs
#from chimerascan.pipeline.extend_sequences import extend_sequences
#from chimerascan.pipeline.sort_discordant_reads import sort_discordant_reads
#from chimerascan.pipeline.nominate_chimeras import nominate_chimeras
#from chimerascan.pipeline.filter_chimeras import filter_encompassing_chimeras
#from chimerascan.pipeline.bedpe_to_fasta import bedpe_to_junction_fasta
#from chimerascan.pipeline.merge_spanning_alignments import merge_spanning_alignments
#from chimerascan.pipeline.filter_spanning_chimeras import filter_spanning_chimeras
#from chimerascan.pipeline.rank_chimeras import rank_chimeras

DEFAULT_NUM_PROCESSORS = config.BASE_PROCESSORS
DEFAULT_KEEP_TMP = False
DEFAULT_BOWTIE_PATH = ""
DEFAULT_BOWTIE_ARGS = "--best --strata"
DEFAULT_MULTIHITS = 100
DEFAULT_MISMATCHES = 2
DEFAULT_SEGMENT_LENGTH = 25
DEFAULT_TRIM5 = 0
DEFAULT_TRIM3 = 0
DEFAULT_MIN_FRAG_LENGTH = 0
DEFAULT_MAX_FRAG_LENGTH = 1000 
DEFAULT_MAX_INDEL_SIZE = 100
DEFAULT_FASTQ_QUAL_FORMAT = SANGER_FORMAT
DEFAULT_LIBRARY_TYPE = LibraryTypes.FR_UNSTRANDED

DEFAULT_HOMOLOGY_MISMATCHES = config.BREAKPOINT_HOMOLOGY_MISMATCHES

DEFAULT_FILTER_MAX_MULTIMAPS = 2
DEFAULT_FILTER_MULTIMAP_RATIO = 0.10
DEFAULT_FILTER_STRAND_PVALUE = 0.01
DEFAULT_FILTER_ISIZE_PERCENTILE = 99.9

DEFAULT_ANCHOR_MIN = 0
DEFAULT_ANCHOR_MAX = 5
DEFAULT_ANCHOR_MISMATCHES = 5
DEFAULT_EMPIRICAL_PROB = 1.0

NUM_POSITIONAL_ARGS = 4

def check_fastq_files(parser, fastq_files, segment_length, trim5, trim3):
    """
    Assures FASTQ files exist and have long enough reads to be
    trimmed by 'trim5' and 'trim3' bases on 5' and 3' ends, respectively,
    to produce reads of 'segment_length'
    """
    # check that input fastq files exist
    read_lengths = []
    for mate,fastq_file in enumerate(fastq_files):
        if not os.path.isfile(fastq_file):
            parser.error("mate '%d' fastq file '%s' is not valid" % 
                         (mate, fastq_file))
        logging.debug("Checking read length for file %s" % (fastq_file))
        read_lengths.append(get_read_length(fastq_file))
        logging.debug("Read length for file %s: %d" % 
                      (fastq_file, read_lengths[-1]))
    # check that mate read lengths are equal
    if len(set(read_lengths)) > 1:
        logging.error("read lengths mate1=%d and mate2=%d are unequal" % 
                      (read_lengths[0], read_lengths[1]))
        return False
    rlen = read_lengths[0]
    trimmed_rlen = rlen - trim5 - trim3
    # check that segment length >= MIN_SEGMENT_LENGTH 
    if segment_length < MIN_SEGMENT_LENGTH:
        parser.error("segment length (%d) too small (min is %d)" % 
                     (segment_length, MIN_SEGMENT_LENGTH))
    # check that segment length < trimmed read length
    if segment_length > trimmed_rlen:
        parser.error("segment length (%d) longer than trimmed read length (%d)" % 
                     (segment_length, trimmed_rlen))
    logging.debug("Segmented alignment will use effective read length of %d" % 
                  (segment_length))        
    return read_lengths[0]

def check_command_line_args(options, args, parser):
    # check command line arguments
    if len(args) < NUM_POSITIONAL_ARGS:
        parser.error("Incorrect number of command line arguments")
    index_dir = args[0]
    fastq_files = args[1:3]
    output_dir = args[3]
    # check that input fastq files exist
    read_lengths = []
    for mate,fastq_file in enumerate(fastq_files):
        if not os.path.isfile(args[0]):
            parser.error("mate '%d' fastq file '%s' is not valid" % 
                         (mate, fastq_file))
        logging.debug("Checking read length for file %s" % 
                      (fastq_file))
        read_lengths.append(get_read_length(fastq_file))
        logging.debug("Read length for file %s: %d" % 
                      (fastq_file, read_lengths[-1]))
    # check that mate read lengths are equal
    if len(set(read_lengths)) > 1:
        parser.error("read lengths mate1=%d and mate2=%d are unequal" % 
                     (read_lengths[0], read_lengths[1]))
    # check that seed length < read length
    if any(options.segment_length > rlen for rlen in read_lengths):
        parser.error("seed length %d cannot be longer than read length" % 
                     (options.segment_length))
    # perform additional checks to ensure fastq files can be segmented
    check_fastq_files(parser, fastq_files, options.segment_length, 
                      options.trim5, options.trim3)
    # check that output dir is not a regular file
    if os.path.exists(output_dir) and (not os.path.isdir(output_dir)):
        parser.error("Output directory name '%s' exists and is not a valid directory" % 
                     (output_dir))
    if check_executable(os.path.join(options.bowtie_path, "bowtie-build")):
        logging.debug("Checking for 'bowtie-build' binary... found")
    else:
        parser.error("bowtie-build binary not found or not executable")
    # check that bowtie program exists
    if check_executable(os.path.join(options.bowtie_path, "bowtie")):
        logging.debug("Checking for 'bowtie' binary... found")
    else:
        parser.error("bowtie binary not found or not executable")
    # check that alignment index exists
    if os.path.isdir(index_dir):
        logging.debug("Checking for chimerascan index directory... found")
    else:
        parser.error("chimerascan alignment index directory '%s' not valid" % 
                     (index_dir))
    # check that alignment index file exists
    align_index_file = os.path.join(index_dir, config.BOWTIE_INDEX_FILE)
    if os.path.isfile(align_index_file):
        logging.debug("Checking for bowtie index file... found")
    else:
        parser.error("chimerascan bowtie index file '%s' invalid" % (align_index_file))
    # check for sufficient processors
    if options.num_processors < config.BASE_PROCESSORS:
        logging.warning("Please specify >=2 processes using '-p' to allow program to run efficiently")

class RunConfig(object):
    
    attrs = (("num_processors", int, DEFAULT_NUM_PROCESSORS),
             ("quals", str, DEFAULT_FASTQ_QUAL_FORMAT),
             ("keep_tmp", parse_bool, DEFAULT_KEEP_TMP),
             ("bowtie_path", str, DEFAULT_BOWTIE_PATH),
             ("bowtie_args", str, DEFAULT_BOWTIE_ARGS),
             ("multihits", int, DEFAULT_MULTIHITS),
             ("mismatches", int, DEFAULT_MISMATCHES),
             ("segment_length", int, DEFAULT_SEGMENT_LENGTH),
             ("trim5", int, DEFAULT_TRIM5),
             ("trim3", int, DEFAULT_TRIM3),
             ("min_fragment_length", int, DEFAULT_MIN_FRAG_LENGTH),
             ("max_fragment_length", int, DEFAULT_MAX_FRAG_LENGTH),
             ("max_indel_size", int, DEFAULT_MAX_INDEL_SIZE),
             ("library_type", str, DEFAULT_LIBRARY_TYPE),
             ("homology_mismatches", int, DEFAULT_HOMOLOGY_MISMATCHES),
             ("filter_max_multimaps", int, DEFAULT_FILTER_MAX_MULTIMAPS),
             ("filter_multimap_ratio", float, DEFAULT_FILTER_MULTIMAP_RATIO),
             ("filter_isize_percentile", float, DEFAULT_FILTER_ISIZE_PERCENTILE),
             ("filter_strand_pval", float, DEFAULT_FILTER_STRAND_PVALUE),
             ("anchor_min", int, DEFAULT_ANCHOR_MIN),
             ("anchor_max", int, DEFAULT_ANCHOR_MAX),
             ("anchor_mismatches", int, DEFAULT_ANCHOR_MISMATCHES),
             ("empirical_prob", float, DEFAULT_EMPIRICAL_PROB))

    def __init__(self):
        self.output_dir = None
        self.fastq_files = None
        self.index_dir = None
        for attrname, attrtype, attrdefault in self.attrs:
            setattr(self, attrname, None)

    def from_xml(self, xmlfile):
        tree = etree.parse(xmlfile)        
        root = tree.getroot()
        # required arguments        
        self.output_dir = root.findtext('output_dir')
        fastq_files = {}
        for mate_elem in root.findall("fastq_files/*"):
            mate = int(mate_elem.get("mate"))
            fastq_files[mate] = mate_elem.text
        self.fastq_files = [fastq_files[mate] for mate in xrange(len(fastq_files))]
        self.index_dir = root.findtext('index')        
        # optional arguments
        for attrname, attrtype, attrdefault in self.attrs:
            val = root.findtext(attrname, attrdefault)            
            setattr(self, attrname, attrtype(val))
    
    def to_xml(self):
        root = etree.Element("chimerascan_config")
        # output dir
        elem = etree.SubElement(root, "output_dir")
        elem.text = self.output_dir   
        # fastq files
        elem = etree.SubElement(root, "fastq_files")
        for mate,fastq_file in enumerate(self.fastq_files):
            file_elem = etree.SubElement(elem, "file", mate=str(mate))
            file_elem.text = fastq_file        
        # index
        elem = etree.SubElement(root, "index")
        elem.text = self.index_dir
        # optional arguments
        for attrname, attrtype, attrdefault in self.attrs:
            val = getattr(self, attrname)
            elem = etree.SubElement(root, attrname)
            elem.text = str(val)
        # indent for pretty printing
        indent(root)        
        return etree.tostring(root)

    @staticmethod
    def get_option_parser():
        parser = OptionParser(usage="%prog [options] [--config <config_file> "
                              " | <index> <mate1.fq> <mate2.fq> <output_dir>]",
                              version="%s" % __version__)
        # standard options
        parser.add_option("--config", dest="config_file",
                          help="Path to configuration XML file") 
        parser.add_option("-v", "--verbose", dest="verbose",
                          action="store_true", default=False,
                          help="enable verbose logging output "
                          "[default=%default]")
        parser.add_option("-p", "--processors", dest="num_processors", 
                          type="int", default=DEFAULT_NUM_PROCESSORS,
                          help="Number of processor cores to allocate to "
                          "chimerascan [default=%default]")
        parser.add_option("--keep-tmp", dest="keep_tmp", action="store_true", 
                          default=DEFAULT_KEEP_TMP,
                          help="Do not delete intermediate files from run")
        parser.add_option("--quals", dest="quals",
                          choices=FASTQ_QUAL_FORMATS, 
                          default=DEFAULT_FASTQ_QUAL_FORMAT, metavar="FMT",
                          help="FASTQ quality score format [default=%default]")
        # alignment options
        bowtie_group = OptionGroup(parser, "Bowtie options",
                                   "Adjust these options to change "
                                   "bowtie alignment settings")
        bowtie_group.add_option("--bowtie-path",
                                dest="bowtie_path",
                                default=DEFAULT_BOWTIE_PATH,
                                help="Path to directory containing 'bowtie' "
                                "[default: assumes bowtie is in the current "
                                "PATH]")
        bowtie_group.add_option("--multihits", type="int", dest="multihits", 
                          default=DEFAULT_MULTIHITS, metavar="MMAX",
                          help="Ignore reads that map to more than MMAX "
                          "locations [default=%default]")
        bowtie_group.add_option("--mismatches", type="int", dest="mismatches",
                          default=DEFAULT_MISMATCHES, metavar="N",
                          help="Aligned reads must have <= N mismatches "
                          "[default=%default]")
        bowtie_group.add_option("--segment-length", type="int", dest="segment_length", 
                          default=DEFAULT_SEGMENT_LENGTH,
                          help="Size of read segments during discordant " 
                          "alignment phase [default=%default]")
        bowtie_group.add_option("--bowtie-args", dest="bowtie_args",
                                default=DEFAULT_BOWTIE_ARGS,
                                help="Additional arguments to pass to bowtie "
                                "aligner (see Bowtie manual for options) "
                                "[default='%default']")               
        bowtie_group.add_option("--trim5", type="int", dest="trim5", 
                          default=DEFAULT_TRIM5, metavar="N",
                          help="Trim N bases from 5' end of read")
        bowtie_group.add_option("--trim3", type="int", dest="trim3", 
                          default=DEFAULT_TRIM3, metavar="N",
                          help="Trim N bases from 3' end of read")
        bowtie_group.add_option("--min-fragment-length", type="int", 
                          dest="min_fragment_length", 
                          default=DEFAULT_MIN_FRAG_LENGTH,
                          help="Smallest expected fragment length "
                          "[default=%default]")
        bowtie_group.add_option("--max-fragment-length", type="int", 
                          dest="max_fragment_length", 
                          default=DEFAULT_MAX_FRAG_LENGTH,
                          help="Largest expected fragment length (reads less"
                          " than this fragment length are assumed to be "
                          " unspliced and contiguous) [default=%default]")
        bowtie_group.add_option('--library', dest="library_type", 
                                choices=LibraryTypes.choices(),
                                default=DEFAULT_LIBRARY_TYPE,
                                help="Library type [default=%default]")
        parser.add_option_group(bowtie_group)
        # filtering options
        filter_group = OptionGroup(parser, "Filtering options",
                                   "Adjust these options to change "
                                   "filtering behavior")
        filter_group.add_option("--homology-mismatches", type="int", 
                                dest="homology_mismatches",
                                default=DEFAULT_HOMOLOGY_MISMATCHES,
                                help="Number of mismatches to tolerate at "
                                "breakpoints when computing homology "
                                "[default=%default]", metavar="N")
        filter_group.add_option("--max-indel-size", type="int", 
                                dest="max_indel_size", 
                                default=DEFAULT_MAX_INDEL_SIZE,
                                help="Tolerate indels less than N bp "
                                "[default=%default]", metavar="N")
        filter_group.add_option("--anchor-min", type="int", dest="anchor_min", 
                          default=DEFAULT_ANCHOR_MIN,
                          help="Minimum junction overlap required to call "
                          "spanning reads [default=%default]")
        filter_group.add_option("--anchor-max", type="int", dest="anchor_max", 
                          default=DEFAULT_ANCHOR_MAX,
                          help="Junction overlap below which to enforce "
                          "mismatch checks [default=%default]")
        filter_group.add_option("--anchor-mismatches", type="int", dest="anchor_mismatches", 
                          default=DEFAULT_ANCHOR_MISMATCHES,
                          help="Number of mismatches allowed within anchor "
                          "region [default=%default]")        
        filter_group.add_option("--filter-multimaps", type="int",
                                dest="filter_max_multimaps",
                                default=DEFAULT_FILTER_MAX_MULTIMAPS,
                                help="Filter chimeras that lack a read "
                                " with <= HITS alternative hits in both "
                                " pairs [default=%default]")
        filter_group.add_option("--filter-multimap-ratio", type="float",
                                default=DEFAULT_FILTER_MULTIMAP_RATIO,
                                dest="filter_multimap_ratio", metavar="RATIO",
                                help="Filter chimeras with a weighted coverage "
                                "versus total reads ratio <= RATIO "
                                "[default=%default]")
        filter_group.add_option("--filter-isize-prob", type="int",
                                default=DEFAULT_FILTER_ISIZE_PERCENTILE,
                                dest="filter_isize_percentile", metavar="N",
                                help="Filter chimeras when putative insert "
                                "size is larger than the (1-N) percentile "
                                "of the distribution [default=%default]")
        filter_group.add_option("--filter-strand-pvalue", type="float",
                                default=DEFAULT_FILTER_STRAND_PVALUE,
                                dest="filter_strand_pval", metavar="p",
                                help="p-value to reject chimera based on "
                                " binomial test that balance of +/- strand "
                                " encompassing reads should be 50/50 "
                                "[default=%default]")
        filter_group.add_option("--empirical-prob", type="float", metavar="p", 
                                dest="empirical_prob", default=DEFAULT_EMPIRICAL_PROB, 
                                help="empirical probability threshold for "
                                "outputting chimeras [default=%default]")        
        parser.add_option_group(filter_group)
        return parser        

    def from_args(self, args, parser=None):
        if parser is None:
            parser = self.get_option_parser()
        options, args = parser.parse_args(args=args)
        # parse config file options/args
        if options.config_file is not None:
            self.from_xml(options.config_file)
        # reset logging to verbose
        if options.verbose:
            logging.getLogger().setLevel(logging.DEBUG)
        # check command line arguments
        if self.index_dir is None:
            self.index_dir = args[0]            
        if self.fastq_files is None:
            if len(args) < 2:
                parser.error("fastq files not specified in config file or command line")        
            self.fastq_files = args[1:3]
        if self.output_dir is None:
            if len(args) < 3:
                parser.error("output dir not specified in config file or command line")   
            self.output_dir = args[3]
        # optional arguments
        # set rest of options, overriding if attribute is undefined
        # or set to something other than the default 
        for attrname, attrtype, attrdefault in self.attrs:
            if ((getattr(self, attrname) is None) or
                (getattr(options, attrname) != attrdefault)):
                setattr(self, attrname, getattr(options, attrname))        

    def check_config(self):
        # check that input fastq files exist
        config_passed = True
        read_lengths = []
        for mate,fastq_file in enumerate(self.fastq_files):
            if not os.path.isfile(fastq_file):
                logging.error("mate '%d' fastq file '%s' is not valid" % 
                              (mate, fastq_file))
                config_passed = False
            read_lengths.append(get_read_length(fastq_file))
            logging.debug("Checking file %s" % (fastq_file))
            logging.debug("File %s read length=%d" % (fastq_file, read_lengths[-1]))
        # check that mate read lengths are equal
        if len(set(read_lengths)) > 1:
            logging.error("Unequal read lengths mate1=%d and mate2=%d" % 
                          (read_lengths[0], read_lengths[1]))
            config_passed = False
        # check that seed length < read length
        if any(self.segment_length > rlen for rlen in read_lengths):
            logging.error("seed length %d cannot be longer than read length" % 
                         (self.segment_length))
            config_passed = False
        # check that output dir is not a regular file
        if os.path.exists(self.output_dir) and (not os.path.isdir(self.output_dir)):
            logging.error("Output directory name '%s' exists and is not a valid directory" % 
                          (self.output_dir))
            config_passed = False
        if check_executable(os.path.join(self.bowtie_path, "bowtie-build")):
            logging.debug("Checking for 'bowtie-build' binary... found")
        else:
            logging.error("bowtie-build binary not found or not executable")
            config_passed = False
        # check that bowtie program exists
        if check_executable(os.path.join(self.bowtie_path, "bowtie")):
            logging.debug("Checking for 'bowtie' binary... found")
        else:
            logging.error("bowtie binary not found or not executable")
            config_passed = False
        # check that alignment index exists
        if os.path.isdir(self.index_dir):
            logging.debug("Checking for chimerascan index directory... found")
            # check that alignment index file exists
            align_index_file = os.path.join(self.index_dir, config.BOWTIE_INDEX_FILE)
            if os.path.isfile(align_index_file):
                logging.debug("Checking for bowtie index file... found")
            else:
                logging.error("chimerascan bowtie index file '%s' invalid" % (align_index_file))
                config_passed = False
        else:
            logging.error("chimerascan alignment index directory '%s' not valid" % 
                          (self.index_dir))
            config_passed = False
        # check for sufficient processors
        if self.num_processors < config.BASE_PROCESSORS:
            logging.warning("Please specify >=2 processes using '-p' to allow program to run efficiently")
        return config_passed

def up_to_date(outfile, infile):
    if not os.path.exists(infile):
        return False
    if not os.path.exists(outfile):
        return False
    if os.path.getsize(outfile) == 0:
        return False    
    return os.path.getmtime(outfile) >= os.path.getmtime(infile)

def run_chimerascan(runconfig):
    # normal run
    config_passed = runconfig.check_config()
    if not config_passed:
        logging.error("Invalid run configuration, aborting.")
        sys.exit(JOB_ERROR)
    # create output dir if it does not exist
    if not os.path.exists(runconfig.output_dir):
        os.makedirs(runconfig.output_dir)
        logging.info("Created output directory: %s" % (runconfig.output_dir))
    # create log dir if it does not exist
    log_dir = os.path.join(runconfig.output_dir, config.LOG_DIR)
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
        logging.debug("Created directory for log files: %s" % (log_dir))        
    # create tmp dir if it does not exist
    tmp_dir = os.path.join(runconfig.output_dir, config.TMP_DIR)
    if not os.path.exists(tmp_dir):
        os.makedirs(tmp_dir)
        logging.debug("Created directory for tmp files: %s" % (tmp_dir))
    # write the run config to a file
    xmlstring = runconfig.to_xml()
    runconfig_xml_file = os.path.join(runconfig.output_dir, config.RUNCONFIG_XML_FILE)
    fh = open(runconfig_xml_file, "w")
    print >>fh, xmlstring
    fh.close()
    # gather and parse run parameters
    gene_feature_file = os.path.join(runconfig.index_dir, config.GENE_FEATURE_FILE)
    bowtie_index = os.path.join(runconfig.index_dir, config.ALIGN_INDEX)
    bowtie_bin = os.path.join(runconfig.bowtie_path, "bowtie")
    bowtie_build_bin = os.path.join(runconfig.bowtie_path, "bowtie-build")
    original_read_length = get_read_length(runconfig.fastq_files[0])
    # minimum fragment length cannot be smaller than the trimmed read length
    trimmed_read_length = original_read_length - runconfig.trim5 - runconfig.trim3
    min_fragment_length = max(runconfig.min_fragment_length, 
                              trimmed_read_length)
    #
    # TODO: trim reads with polyA tails or bad quality scores
    #
    # skip this step for now
    #
    # Initial Bowtie alignment step
    #
    # align in paired-end mode, trying to resolve as many reads as possible
    # this effectively rules out the vast majority of reads as candidate
    # fusions
    unaligned_fastq_param = os.path.join(tmp_dir, config.UNALIGNED_FASTQ_PARAM)
    maxmultimap_fastq_param = os.path.join(tmp_dir, config.MAXMULTIMAP_FASTQ_PARAM)
    aligned_bam_file = os.path.join(runconfig.output_dir, config.ALIGNED_READS_BAM_FILE)
    aligned_log_file = os.path.join(log_dir, "bowtie_alignment.log")    
    msg = "Aligning reads with bowtie"
    if all(up_to_date(aligned_bam_file, fq) for fq in runconfig.fastq_files):
        logging.info("[SKIPPED] %s" % (msg))
    else:    
        logging.info(msg)
        retcode = align_pe(runconfig.fastq_files, 
                           bowtie_index,
                           aligned_bam_file, 
                           unaligned_fastq_param,
                           maxmultimap_fastq_param,
                           min_fragment_length=min_fragment_length,
                           max_fragment_length=runconfig.max_fragment_length,
                           trim5=runconfig.trim5,
                           trim3=runconfig.trim3,
                           library_type=runconfig.library_type,
                           num_processors=runconfig.num_processors,
                           quals=runconfig.quals,
                           multihits=runconfig.multihits,
                           mismatches=runconfig.mismatches,
                           bowtie_bin=bowtie_bin,
                           bowtie_args=runconfig.bowtie_args,
                           log_file=aligned_log_file,
                           keep_unmapped=False)
        if retcode != 0:
            logging.error("Bowtie failed with error code %d" % (retcode))    
            sys.exit(retcode)
    #
    # Get insert size distribution
    #
    isize_dist_file = os.path.join(runconfig.output_dir, config.ISIZE_DIST_FILE)
    isize_dist = InsertSizeDistribution()
    if up_to_date(isize_dist_file, aligned_bam_file):
        logging.info("[SKIPPED] Profiling insert size distribution")
        isize_dist.from_file(open(isize_dist_file, "r"))
    else:
        logging.info("Profiling insert size distribution")
        max_isize_samples = config.ISIZE_MAX_SAMPLES
        bamfh = pysam.Samfile(aligned_bam_file, "rb")
        isize_dist.from_bam(bamfh, min_isize=min_fragment_length, 
                            max_isize=runconfig.max_fragment_length, 
                            max_samples=max_isize_samples)
        isize_dist.to_file(open(isize_dist_file, "w"))
        bamfh.close()
    logging.info("Insert size samples=%d mean=%f std=%f median=%d mode=%d" % 
                 (isize_dist.n, isize_dist.mean(), isize_dist.std(), 
                  isize_dist.percentile(50.0), isize_dist.mode()))
    #
    # Realignment step
    #
    # trim and realign all the initially unaligned reads, in order to
    # increase sensitivity to detect reads spanning fusion junctions
    #
    realigned_bam_file = os.path.join(tmp_dir, config.REALIGNED_BAM_FILE)
    realigned_log_file = os.path.join(log_dir, "bowtie_trimmed_realignment.log")
    unaligned_fastq_files = [os.path.join(tmp_dir, fq) 
                             for fq in config.UNALIGNED_FASTQ_FILES]
    msg = "Trimming and re-aligning initially unmapped reads"
    if all(up_to_date(realigned_bam_file, fq) for fq in runconfig.fastq_files):
        logging.info("[SKIPPED] %s" % (msg))
    else:
        logging.info(msg)
        trim_align_pe_sr(unaligned_fastq_files,
                         bowtie_index,
                         realigned_bam_file,
                         unaligned_fastq_param=None,
                         maxmultimap_fastq_param=None,
                         trim5=runconfig.trim5,
                         library_type=runconfig.library_type,
                         num_processors=runconfig.num_processors,
                         quals=runconfig.quals,
                         multihits=runconfig.multihits, 
                         mismatches=runconfig.mismatches,
                         bowtie_bin=bowtie_bin,
                         bowtie_args=runconfig.bowtie_args,
                         log_file=realigned_log_file,
                         segment_length=runconfig.segment_length,
                         keep_unmapped=True)
    #
    # Find discordant reads
    #
    # iterate through realigned reads and divide them into groups of
    # concordant, discordant within a gene (isoforms), discordant
    # between different genes, and discordant in the genome
    #
    gene_paired_bam_file = os.path.join(tmp_dir, config.GENE_PAIRED_BAM_FILE)
    genome_paired_bam_file = os.path.join(tmp_dir, config.GENOME_PAIRED_BAM_FILE)
    realigned_unmapped_bam_file = os.path.join(tmp_dir, config.REALIGNED_UNMAPPED_BAM_FILE)
    realigned_complex_bam_file = os.path.join(tmp_dir, config.REALIGNED_COMPLEX_BAM_FILE)
    msg = "Classifying concordant and discordant read pairs"
    if (up_to_date(gene_paired_bam_file, realigned_bam_file) and
        up_to_date(genome_paired_bam_file, realigned_bam_file) and
        up_to_date(realigned_unmapped_bam_file, realigned_bam_file) and
        up_to_date(realigned_complex_bam_file, realigned_bam_file)):        
        logging.info("[SKIPPED] %s" % (msg))
    else:
        logging.info(msg)
        find_discordant_fragments(realigned_bam_file, 
                                  gene_paired_bam_file=gene_paired_bam_file, 
                                  genome_paired_bam_file=genome_paired_bam_file, 
                                  unmapped_bam_file=realigned_unmapped_bam_file,
                                  complex_bam_file=realigned_complex_bam_file,
                                  index_dir=runconfig.index_dir,
                                  max_isize=runconfig.max_fragment_length,
                                  library_type=runconfig.library_type) 
    #
    # Nominate chimeric genes
    #
    # iterate through pairs of reads and nominate chimeras
    #
    encompassing_chimera_file = os.path.join(tmp_dir, config.ENCOMPASSING_CHIMERA_FILE)
    msg = "Nominating chimeras from discordant reads"
    if (up_to_date(encompassing_chimera_file, gene_paired_bam_file)):
        logging.info("[SKIPPED] %s" % (msg))
    else:        
        logging.info(msg)
        nominate_chimeras(runconfig.index_dir, 
                          gene_paired_bam_file, 
                          encompassing_chimera_file, 
                          trim_bp=config.EXON_JUNCTION_TRIM_BP)
    #
    # Determine putative breakpoint sequences for chimera candidates
    # and group chimeras together if they share the same breakpoint
    #
    breakpoint_chimera_file = os.path.join(tmp_dir, config.BREAKPOINT_CHIMERA_FILE)
    breakpoint_map_file = os.path.join(tmp_dir, config.BREAKPOINT_MAP_FILE)
    breakpoint_fasta_file = os.path.join(tmp_dir, config.BREAKPOINT_FASTA_FILE)
    msg = "Extracting breakpoint sequences from chimeras"
    if (up_to_date(breakpoint_chimera_file, encompassing_chimera_file) and
        up_to_date(breakpoint_map_file, encompassing_chimera_file) and
        up_to_date(breakpoint_fasta_file, encompassing_chimera_file)):
        logging.info("[SKIPPED] %s" % (msg))
    else:
        logging.info(msg)
        determine_chimera_breakpoints(index_dir=runconfig.index_dir, 
                                      read_length=trimmed_read_length, 
                                      input_chimera_file=encompassing_chimera_file,
                                      output_chimera_file=breakpoint_chimera_file, 
                                      breakpoint_map_file=breakpoint_map_file,
                                      breakpoint_fasta_file=breakpoint_fasta_file,
                                      homology_mismatches=runconfig.homology_mismatches)
    #
    # Build a bowtie index to align and detect spanning reads
    #
    breakpoint_bowtie_index = os.path.join(tmp_dir, config.BREAKPOINT_BOWTIE_INDEX)
    breakpoint_bowtie_index_file = os.path.join(tmp_dir, config.BREAKPOINT_BOWTIE_INDEX_FILE)
    msg = "Building bowtie index of breakpoint spanning sequences"
    if (up_to_date(breakpoint_bowtie_index_file, breakpoint_fasta_file)):
        logging.info("[SKIPPED] %s" % (msg))
    else:
        logging.info(msg)
        args = [bowtie_build_bin, breakpoint_fasta_file, 
                breakpoint_bowtie_index]
        f = open(os.path.join(log_dir, "breakpoint_bowtie_index.log"), "w")
        retcode = subprocess.call(args, stdout=f, stderr=f)
        f.close()
        if retcode != config.JOB_SUCCESS:
            logging.error("'bowtie-build' failed to create breakpoint index")
            if os.path.exists(breakpoint_bowtie_index_file):
                os.remove(breakpoint_bowtie_index_file)
            return config.JOB_ERROR
    #
    # Extract reads to realign against breakpoint sequences
    #
    spanning_fastq_file = os.path.join(tmp_dir, config.SPANNING_FASTQ_FILE)
    msg = "Extracting putative breakpoint-spanning reads"
    if (up_to_date(spanning_fastq_file, realigned_unmapped_bam_file) and
        up_to_date(spanning_fastq_file, breakpoint_chimera_file)):
        logging.info("[SKIPPED] %s" % (msg))
    else:
        logging.info(msg)    
        retcode = nominate_spanning_reads(chimera_file=breakpoint_chimera_file, 
                                          unmapped_bam_file=realigned_unmapped_bam_file, 
                                          output_fastq_file=spanning_fastq_file)
        if retcode != config.JOB_SUCCESS:
            logging.error("Failed to extract breakpoint-spanning reads")
            if os.path.exists(spanning_fastq_file):
                os.remove(spanning_fastq_file)
            return config.JOB_ERROR
    #
    # Align reads against breakpoint junctions
    # 
    spanning_bam_file = os.path.join(tmp_dir, config.SPANNING_BAM_FILE)
    spanning_log_file = os.path.join(log_dir, "bowtie_spanning_alignment.log")        
    msg = "Aligning junction spanning reads"
    if (up_to_date(spanning_bam_file, breakpoint_bowtie_index_file) and
        up_to_date(spanning_bam_file, spanning_fastq_file)):
        logging.info("[SKIPPED] %s" % (msg))
    else:            
        logging.info(msg)
        retcode= align_sr(spanning_fastq_file, 
                          bowtie_index=breakpoint_bowtie_index,
                          output_bam_file=spanning_bam_file, 
                          unaligned_fastq_param=None,
                          maxmultimap_fastq_param=None,
                          trim5=0,
                          trim3=0,
                          library_type=runconfig.library_type,
                          num_processors=runconfig.num_processors, 
                          quals=SANGER_FORMAT,
                          multihits=runconfig.multihits,
                          mismatches=runconfig.mismatches, 
                          bowtie_bin=bowtie_bin,
                          bowtie_args=runconfig.bowtie_args,
                          log_file=spanning_log_file,
                          keep_unmapped=False)
        if retcode != config.JOB_SUCCESS:
            logging.error("Bowtie failed with error code %d" % (retcode))    
            return config.JOB_ERROR
    #
    # Merge spanning read alignments into chimera file
    #
    spanning_chimera_file = os.path.join(tmp_dir, config.SPANNING_CHIMERA_FILE)
    msg = "Merging spanning reads information with chimeras"
    if (up_to_date(spanning_chimera_file, breakpoint_chimera_file) and
        up_to_date(spanning_chimera_file, spanning_bam_file)):
        logging.info("[SKIPPED] %s" % (msg))
    else:
        logging.info(msg)
        merge_spanning_alignments(junc_bam_file, junc_map_file, 
                                  raw_chimera_bedpe_file,
                                  anchor_min=0, 
                                  anchor_max=0,
                                  anchor_mismatches=0)

    return
    #
    # Choose best isoform for each junction
    #
    chimera_bedpe_file = os.path.join(tmp_dir, config.CHIMERA_BEDPE_FILE)
    if (up_to_date(chimera_bedpe_file, raw_chimera_bedpe_file)):
        logging.info("[SKIPPED] Filtering chimeras")
    else:
        logging.info("Filtering chimeras")
        # get insert size at prob    
        max_isize = isize_dist.percentile(runconfig.filter_isize_percentile)
        filter_spanning_chimeras(raw_chimera_bedpe_file, 
                                 chimera_bedpe_file,
                                 gene_feature_file,
                                 mate_pval=runconfig.filter_strand_pval,
                                 max_isize=max_isize)
    #
    # Rank chimeras
    #
    ranked_chimera_bedpe_file = os.path.join(runconfig.output_dir, 
                                             config.RANKED_CHIMERA_BEDPE_FILE)
    if (up_to_date(ranked_chimera_bedpe_file, chimera_bedpe_file)):
        logging.info("[SKIPPED] Ranking chimeras")
    else:
        logging.info("Ranking chimeras")
        rank_chimeras(chimera_bedpe_file, ranked_chimera_bedpe_file,
                      empirical_prob=runconfig.empirical_prob)
    #
    # Cleanup
    # 
    #shutil.rmtree(tmp_dir)
    #
    # Done
    #    
    logging.info("Finished run. Chimeras written to file %s" %
                 (ranked_chimera_bedpe_file))
    return JOB_SUCCESS


    #
    # Filter encompassing chimeras step
    #
    # TODO: might be best to do this at the very end
#    filtered_encomp_bedpe_file = \
#        os.path.join(tmp_dir,
#                     config.FILTERED_ENCOMPASSING_CHIMERA_BEDPE_FILE)
#    if (up_to_date(filtered_encomp_bedpe_file, encompassing_bedpe_file)):
#        logging.info("[SKIPPED] Filtering encompassing chimeras")
#    else:
#        logging.info("Filtering encompassing chimeras")
#        # max_isize = isize_mean + runconfig.filter_isize_stdevs*isize_std
#        filter_encompassing_chimeras(encompassing_bedpe_file,
#                                     filtered_encomp_bedpe_file,
#                                     gene_feature_file,
#                                     max_multimap=runconfig.filter_max_multimaps,
#                                     multimap_cov_ratio=runconfig.filter_multimap_ratio,
#                                     max_isize=-1,
#                                     strand_pval=runconfig.filter_strand_pval)
    #
    # Nominate spanning reads step
    #
#    spanning_fastq_file = os.path.join(runconfig.output_dir, 
#                                       config.SPANNING_FASTQ_FILE)
#    if all(up_to_date(spanning_fastq_file, f) for f in unaligned_fastq_files):
#        logging.info("[SKIPPED] Preparing junction spanning reads")
#    else:
#        logging.info("Preparing junction spanning reads")
#        outfh = open(spanning_fastq_file, "w")
#        for f in unaligned_fastq_files:
#            shutil.copyfileobj(open(f), outfh)
#        outfh.close()        
    # TODO: skip this step for now, and simply realign all the reads
#    spanning_fastq_file = os.path.join(runconfig.output_dir, config.SPANNING_FASTQ_FILE)
#    if (up_to_date(spanning_fastq_file, extended_discordant_bedpe_file) and 
#        up_to_date(spanning_fastq_file, filtered_encomp_bedpe_file)):
#        logging.info("[SKIPPED] Nominating junction spanning reads")
#    else:
#        logging.info("Nominating junction spanning reads")
#        nominate_spanning_reads(open(extended_discordant_bedpe_file, 'r'),
#                                open(filtered_encomp_bedpe_file, 'r'),
#                                open(spanning_fastq_file, 'w'))    
    #
    # Extract junction sequences from chimeras file
    #        
    ref_fasta_file = os.path.join(runconfig.index_dir, config.ALIGN_INDEX + ".fa")
    junc_fasta_file = os.path.join(tmp_dir, config.JUNC_REF_FASTA_FILE)
    junc_map_file = os.path.join(tmp_dir, config.JUNC_REF_MAP_FILE)
    spanning_read_length = get_read_length(spanning_fastq_file)    
    if (up_to_date(junc_fasta_file, filtered_encomp_bedpe_file) and
        up_to_date(junc_map_file, filtered_encomp_bedpe_file)):        
        logging.info("[SKIPPED] Extracting junction read sequences")
    else:        
        logging.info("Extracting junction read sequences")
        bedpe_to_junction_fasta(filtered_encomp_bedpe_file, ref_fasta_file,                                
                                spanning_read_length, 
                                open(junc_fasta_file, "w"),
                                open(junc_map_file, "w"))
    #
    # Build a bowtie index to align and detect spanning reads
    #
    bowtie_spanning_index = os.path.join(tmp_dir, config.JUNC_BOWTIE_INDEX)
    bowtie_spanning_index_file = os.path.join(tmp_dir, config.JUNC_BOWTIE_INDEX_FILE)
    if (up_to_date(bowtie_spanning_index_file, junc_fasta_file)):
        logging.info("[SKIPPED] Building bowtie index for junction-spanning reads")
    else:        
        logging.info("Building bowtie index for junction-spanning reads")
        args = [runconfig.bowtie_build_bin, junc_fasta_file, bowtie_spanning_index]
        f = open(os.path.join(log_dir, "bowtie_build.log"), "w")
        subprocess.call(args, stdout=f, stderr=f)
        f.close()
    #
    # Align unmapped reads across putative junctions
    #
    junc_bam_file = os.path.join(tmp_dir, config.JUNC_READS_BAM_FILE)
    junc_log_file = os.path.join(log_dir, "bowtie_spanning_alignment.log")        
    if (up_to_date(junc_bam_file, bowtie_spanning_index_file) and
        up_to_date(junc_bam_file, spanning_fastq_file)):
        logging.info("[SKIPPED] Aligning junction spanning reads")
    else:            
        logging.info("Aligning junction spanning reads")
        retcode = align_sr_full(spanning_fastq_file, 
                                bowtie_spanning_index,
                                junc_bam_file,
                                trim5=runconfig.trim5,
                                trim3=runconfig.trim3,                                 
                                num_processors=runconfig.num_processors,
                                fastq_format=runconfig.fastq_format,
                                multihits=runconfig.multihits,
                                mismatches=runconfig.mismatches,
                                bowtie_bin=runconfig.bowtie_bin,
                                bowtie_mode=bowtie_mode,
                                log_file=junc_log_file)
        if retcode != 0:
            logging.error("Bowtie failed with error code %d" % (retcode))    
            sys.exit(retcode)
    #
    # Merge spanning and encompassing read information
    #
    raw_chimera_bedpe_file = os.path.join(tmp_dir, config.RAW_CHIMERA_BEDPE_FILE)
    if (up_to_date(raw_chimera_bedpe_file, junc_bam_file) and
        up_to_date(raw_chimera_bedpe_file, junc_map_file)):
        logging.info("[SKIPPED] Merging spanning and encompassing read alignments")
    else:
        logging.info("Merging spanning and encompassing read alignments")
        merge_spanning_alignments(junc_bam_file, junc_map_file, 
                                  raw_chimera_bedpe_file,
                                  anchor_min=0, 
                                  anchor_max=0,
                                  anchor_mismatches=0)
    #
    # Choose best isoform for each junction
    #
    chimera_bedpe_file = os.path.join(tmp_dir, config.CHIMERA_BEDPE_FILE)
    if (up_to_date(chimera_bedpe_file, raw_chimera_bedpe_file)):
        logging.info("[SKIPPED] Filtering chimeras")
    else:
        logging.info("Filtering chimeras")
        # get insert size at prob    
        max_isize = isize_dist.percentile(runconfig.filter_isize_percentile)
        filter_spanning_chimeras(raw_chimera_bedpe_file, 
                                 chimera_bedpe_file,
                                 gene_feature_file,
                                 mate_pval=runconfig.filter_strand_pval,
                                 max_isize=max_isize)
    #
    # Rank chimeras
    #
    ranked_chimera_bedpe_file = os.path.join(runconfig.output_dir, 
                                             config.RANKED_CHIMERA_BEDPE_FILE)
    if (up_to_date(ranked_chimera_bedpe_file, chimera_bedpe_file)):
        logging.info("[SKIPPED] Ranking chimeras")
    else:
        logging.info("Ranking chimeras")
        rank_chimeras(chimera_bedpe_file, ranked_chimera_bedpe_file,
                      empirical_prob=runconfig.empirical_prob)
    #
    # Cleanup
    # 
    #shutil.rmtree(tmp_dir)
    #
    # Done
    #    
    logging.info("Finished run. Chimeras written to file %s" %
                 (ranked_chimera_bedpe_file))
    return JOB_SUCCESS

def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    # parse run parameters in config file and command line
    runconfig = RunConfig()
    runconfig.from_args(sys.argv[1:])
    # run chimerascan
    sys.exit(run_chimerascan(runconfig))

if __name__ == '__main__':
    main()
