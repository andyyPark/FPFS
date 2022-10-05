#!/usr/bin/env python
#
# FPFS shear estimator
# Copyright 20220312 Xiangchong Li.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
import os
import fpfs
import schwimmbad
import numpy as np
from fpfs.default import *
import astropy.io.fits as pyfits
from argparse import ArgumentParser
from configparser import ConfigParser

class Worker(object):
    def __init__(self,config_name,gver='g1'):
        cparser     =   ConfigParser()
        cparser.read(config_name)
        # setup processor
        self.catdir =   cparser.get('procsim', 'cat_dir')
        self.simname=   cparser.get('procsim', 'sim_name')
        proc_name   =   cparser.get('procsim', 'proc_name')
        self.do_det =   cparser.getboolean('FPFS', 'do_det')
        self.do_noirev= cparser.getboolean('FPFS', 'do_noirev')
        self.rcut   =   cparser.getint('FPFS', 'rcut')
        self.indir  =   os.path.join(self.catdir,'src_%s_%s'%(self.simname,proc_name))
        if not os.path.exists(self.indir):
            raise FileNotFoundError('Cannot find input directory!')
        print('The input directory for galaxy shear catalogs is %s. ' %self.indir)
        # setup WL distortion parameter
        self.gver   =   gver
        self.Const  =   cparser.getfloat('FPFS', 'weighting_c')
        return

    def run(self,Id):
        pp    = 'cut%d' %self.rcut
        in_nm1= os.path.join(self.indir,'fpfs-%s-%04d-%s-0000.fits' %(pp,Id,self.gver))
        in_nm2= os.path.join(self.indir,'fpfs-%s-%04d-%s-2222.fits' %(pp,Id,self.gver))
        assert os.path.isfile(in_nm1) & os.path.isfile(in_nm2), 'Cannot find\
                input galaxy shear catalog distorted by positive and negative shear'
        mm1   = pyfits.getdata(in_nm1)
        mm2   = pyfits.getdata(in_nm2)
        ellM1 = fpfs.catalog.fpfsM2E(mm1,const=self.Const,noirev=self.do_noirev)
        ellM2 = fpfs.catalog.fpfsM2E(mm2,const=self.Const,noirev=self.do_noirev)

        fs1 =   fpfs.catalog.summary_stats(mm1,ellM1,use_sig=False,ratio=1.)
        fs2 =   fpfs.catalog.summary_stats(mm2,ellM2,use_sig=False,ratio=1.)
        dcc =   -0.6
        cutB=   25.5
        ncut=   10
        # Here I only show an example of cutting on magnitude
        selnm=  ['M00']
        cutsig= [sigM]

        #names= [('cut','<f8'), ('de','<f8'), ('eA1','<f8'), ('eA2','<f8'), ('res1','<f8'), ('res2','<f8')]
        out =   np.zeros((6,ncut))
        for i in range(ncut):
            fs1.clear_outcomes()
            fs2.clear_outcomes()
            mcut=cutB+dcc*i
            cut=[10**((27.-mcut)/2.5)]
            # cut=[]
            # weight array
            fs1.update_selection_weight(selnm,cut,cutsig);fs2.update_selection_weight(selnm,cut,cutsig)
            fs1.update_selection_bias(selnm,cut,cutsig);fs2.update_selection_bias(selnm,cut,cutsig)
            fs1.update_ellsum();fs2.update_ellsum()
            out[0,i]= mcut
            out[1,i]= fs2.sumE1-fs1.sumE1
            out[2,i]= (fs1.sumE1+fs2.sumE1)/2.
            out[3,i]= (fs1.sumE1+fs2.sumE1+fs1.corE1+fs2.corE1)/2.
            out[4,i]= (fs1.sumR1+fs2.sumR1)/2.
            out[5,i]= (fs1.sumR1+fs2.sumR1+fs1.corR1+fs2.corR1)/2.
        return out

    def __call__(self,Id):
        print('start ID: %d' %(Id))
        return self.run(Id)

if __name__=='__main__':
    parser = ArgumentParser(description="fpfs procsim")
    parser.add_argument('--minId', required=True,type=int,
                        help='minimum id number, e.g. 0')
    parser.add_argument('--maxId', required=True,type=int,
                        help='maximum id number, e.g. 4000')
    parser.add_argument('--config', required=True,type=str,
                        help='configure file name')
    group   =   parser.add_mutually_exclusive_group()
    group.add_argument("--ncores", dest="n_cores", default=1,
                       type=int, help="Number of processes (uses multiprocessing).")
    group.add_argument("--mpi", dest="mpi", default=False,
                       action="store_true", help="Run with MPI.")
    args    =   parser.parse_args()
    pool    =   schwimmbad.choose_pool(mpi=args.mpi, processes=args.n_cores)

    cparser     =   ConfigParser()
    cparser.read(args.config)
    glist=[]
    if cparser.getboolean('distortion','test_g1'):
        glist.append('g1')
    if cparser.getboolean('distortion','test_g2'):
        glist.append('g2')
    if len(glist)<1:
        raise ValueError('Cannot test nothing!! Must test g1 or test g2. ')

    for gver in glist:
        print('Testing for %s . ' %gver)
        worker  =   Worker(args.config,gver=gver)
        refs    =   list(range(args.minId,args.maxId))
        outs    =   []
        for r in pool.map(worker,refs):
            outs.append(r)
        outs    =   np.vstack(outs)
        if len(outs.shape)==3:
            outs    =   np.sum(outs,axis=0)

        print('Separate galaxies into %d bins: %s'  %(len(outs[0]),outs[0]))
        print('Multiplicative biases for those bins are: ',(outs[1]/outs[5]/2.-0.02)/0.02)
        del worker
    pool.close()
