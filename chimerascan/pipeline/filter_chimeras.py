'''
Created on Jan 31, 2011

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
import logging
import collections
import os

from chimerascan.lib.gene_to_genome import build_gene_to_genome_map, \
    gene_to_genome_pos, build_tx_cluster_map
from chimerascan.lib.chimera import Chimera
from chimerascan.lib import config
from chimerascan.lib.base import make_temp

def filter_weighted_frags(c, threshold):
    """
    filters chimeras with weighted coverage greater than
    'threshold'.  chimeras with scores less than the threshold
    can pass filter at a lower threshold when spanning reads
    are present
    """
    return c.get_weighted_unique_frags() >= threshold

def filter_inner_dist(c, max_isize):
    '''
    filters chimeras whenever either the 5' or the 3'
    partner has a predicted insert size larger than
    'max_isize' bp
    '''
    if max_isize <= 0:
        return True
    if c.partner5p.inner_dist > max_isize:
        return False
    if c.partner3p.inner_dist > max_isize:
        return False
    return True
    #inner_dist = c.partner5p.inner_dist + c.partner3p.inner_dist
    #return inner_dist <= max_isize

def filter_chimeric_isoform_fraction(c, frac):
    """
    filters chimeras with fewer than 'threshold' total
    unique read alignments
    """ 
    pass

def get_highest_coverage_isoforms(input_file, gene_file):
    # place overlapping chimeras into clusters
    logging.debug("Building isoform cluster lookup table")
    tx_cluster_map = build_tx_cluster_map(open(gene_file))
    # build a lookup table to get genome coordinates from transcript 
    # coordinates
    tx_genome_map = build_gene_to_genome_map(open(gene_file))
    cluster_chimera_dict = collections.defaultdict(lambda: [])
    for c in Chimera.parse(open(input_file)):
        key = (c.name,
               c.get_num_unique_spanning_positions(),
               c.get_weighted_cov(),
               c.get_num_frags())
        # get cluster of overlapping genes
        cluster5p = tx_cluster_map[c.partner5p.tx_name]
        cluster3p = tx_cluster_map[c.partner3p.tx_name]
        # get genomic positions of breakpoints
        coord5p = gene_to_genome_pos(c.partner5p.tx_name, c.partner5p.end-1, tx_genome_map)
        coord3p = gene_to_genome_pos(c.partner3p.tx_name, c.partner3p.start, tx_genome_map)
        # add to dictionary
        cluster_chimera_dict[(cluster5p,cluster3p,coord5p,coord3p)].append(key)    
    # choose highest coverage chimeras within each pair of clusters
    logging.debug("Finding highest coverage isoforms")
    kept_chimeras = set()
    for stats_list in cluster_chimera_dict.itervalues():
        stats_dict = collections.defaultdict(lambda: set())
        for stats_info in stats_list:
            # index chimera names
            stats_dict[stats_info[1:]].add(stats_info[0])
        # find highest scoring key
        sorted_keys = sorted(stats_dict.keys(), reverse=True)
        kept_chimeras.update(stats_dict[sorted_keys[0]])
    return kept_chimeras

def read_false_pos_file(filename):
    false_pos_chimeras = set()
    for line in open(filename):
        fields = line.strip().split("\t")
        tx_name_5p, end5p, tx_name_3p, start3p = fields
        end5p = int(end5p)
        start3p = int(start3p)
        false_pos_chimeras.add((tx_name_5p, end5p, tx_name_3p, start3p))
    return false_pos_chimeras

def filter_chimeras(input_file, output_file,
                    index_dir,
                    weighted_unique_frags,
                    max_isize,
                    false_pos_file):
    if (false_pos_file is not None) and (false_pos_file is not ""):
        logging.debug("Parsing false positive chimeras")
        false_pos_pairs = read_false_pos_file(false_pos_file)
    else:
        false_pos_pairs = set()
    # filter chimeras
    num_chimeras = 0
    num_filtered_chimeras = 0
    tmp_file = make_temp(os.path.dirname(output_file), suffix=".txt")
    f = open(tmp_file, "w")
    logging.debug("Filtering chimeras")
    logging.debug("\tweighted unique fragments: %f" % (weighted_unique_frags))
    logging.debug("\tmax insert size allowed: %d" % (max_isize))
    logging.debug("\tfalse positive chimeras file: %s" % (false_pos_file))
    for c in Chimera.parse(open(input_file)):
        good = filter_weighted_frags(c, weighted_unique_frags)
        good = good and filter_inner_dist(c, max_isize)
        false_pos_key = (c.partner5p.tx_name, c.partner5p.end, 
                         c.partner3p.tx_name, c.partner3p.start)
        good = good and (false_pos_key not in false_pos_pairs)
        if good:
            print >>f, '\t'.join(map(str, c.to_list()))
            num_filtered_chimeras += 1
        num_chimeras += 1
    f.close()
    logging.debug("Total chimeras: %d" % num_chimeras)
    logging.debug("Filtered chimeras: %d" % num_filtered_chimeras)
    # cleanup memory for false positive chimeras
    del false_pos_pairs
    # find highest coverage chimeras among isoforms
    gene_file = os.path.join(index_dir, config.GENE_FEATURE_FILE)
    kept_chimeras = get_highest_coverage_isoforms(tmp_file, gene_file)
    num_filtered_chimeras = 0
    f = open(output_file, "w")
    for c in Chimera.parse(open(input_file)):
        if c.name in kept_chimeras:
            num_filtered_chimeras += 1
            print >>f, '\t'.join(map(str, c.to_list()))
    f.close()
    logging.debug("\tAfter choosing best isoform: %d" % 
                  num_filtered_chimeras)
    os.remove(tmp_file)
    return config.JOB_SUCCESS

def main():
    from optparse import OptionParser
    logging.basicConfig(level=logging.DEBUG,
                        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    parser = OptionParser("usage: %prog [options] <index_dir> <in.txt> <out.txt>")
    parser.add_option("--unique-frags", type="float", default=3.0,
                      dest="weighted_unique_frags", metavar="N",
                      help="Filter chimeras with less than N unique "
                      "aligned fragments (multimapping fragments are "
                      "assigned fractional weights) [default=%default]")
    parser.add_option("--max-isize", type="int", default=1e6,
                      dest="max_isize", metavar="N",
                      help="Filter chimeras when inner distance "
                      "is larger than N bases [default=%default]")
    parser.add_option("--false-pos", dest="false_pos_file",
                      default=None, 
                      help="File containing known false positive "
                      "transcript pairs to subtract from output")
    options, args = parser.parse_args()
    index_dir = args[0]
    input_file = args[1]
    output_file = args[2]
    return filter_chimeras(input_file, output_file, index_dir,
                           weighted_unique_frags=options.weighted_unique_frags,
                           max_isize=options.max_isize,
                           false_pos_file=options.false_pos_file)


if __name__ == "__main__":
    main()