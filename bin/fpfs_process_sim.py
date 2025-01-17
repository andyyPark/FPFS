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
import gc
import jax
import fpfs
import json
import schwimmbad
import numpy as np
import astropy.io.fits as pyfits
from argparse import ArgumentParser
from configparser import ConfigParser

_DefaultImgSize = 6400


class Worker(object):
    def __init__(self, config_name):
        cparser = ConfigParser()
        cparser.read(config_name)
        # setup processor
        self.imgdir = cparser.get("procsim", "img_dir")
        self.catdir = cparser.get("procsim", "cat_dir")
        self.do_det = cparser.getboolean("FPFS", "do_det")
        self.sigma_as = cparser.getfloat("FPFS", "sigma_as")
        self.sigma_det = cparser.getfloat("FPFS", "sigma_det")
        self.rcut = cparser.getint("FPFS", "rcut")
        if not os.path.exists(self.imgdir):
            raise FileNotFoundError("Cannot find input images directory!")
        if not os.path.exists(self.catdir):
            os.makedirs(self.catdir, exist_ok=True)
        print("The output directory for shear catalogs is %s. " % self.catdir)

        # setup survey parameters
        self.scale = cparser.getfloat("survey", "pixel_scale")
        self.psf_fname = cparser.get("procsim", "psf_filename")
        if not os.path.isfile(self.psf_fname):
            raise FileNotFoundError("Cannot find PSF file: %s" % self.psf_fname)
        self.noi_var = cparser.getfloat("survey", "noi_var")
        # size of the image
        self.image_nx = cparser.getint("survey", "image_nx")
        self.image_ny = cparser.getint("survey", "image_ny")
        self.magz = cparser.getfloat("survey", "mag_zero")
        assert self.image_ny == self.image_nx, "image_nx must equals image_ny!"
        assert self.image_ny <= _DefaultImgSize, (
            "image_nx (image_ny) should \
                    not be greater than %d !"
            % _DefaultImgSize
        )

        # setup WL distortion parameter
        glist = []
        # this is for const shear tests
        if cparser.getboolean("distortion", "test_g1"):
            glist.append("g1")
        if cparser.getboolean("distortion", "test_g2"):
            glist.append("g2")
        if len(glist) > 0:
            zlist = json.loads(cparser.get("distortion", "shear_z_list"))
            self.pendList = ["%s_%s" % (i1, i2) for i1 in glist for i2 in zlist]
        else:
            raise ValueError("problem in distortion setup")

        if self.noi_var > 1e-20:
            ngrid = 2 * self.rcut
            self.noise_pow = np.ones((ngrid, ngrid)) * self.noi_var * ngrid**2.0
            self.ncov_fname = os.path.join(self.catdir, "cov_matrix.fits")
        return

    def prepare_psf(self, psf_fname, rcut, ngrid2):
        ngrid = 64
        beg = ngrid // 2 - rcut
        end = beg + 2 * rcut
        psf_data = pyfits.getdata(psf_fname)
        npad = (ngrid - psf_data.shape[0]) // 2
        psf_data2 = np.pad(
            psf_data,
            (npad + 1, npad),
            mode="constant",
        )[beg:end, beg:end]
        del npad
        npad = (ngrid2 - psf_data.shape[0]) // 2
        psf_data3 = np.pad(psf_data, (npad + 1, npad), mode="constant")
        return psf_data2, psf_data3

    def run(self, imid):
        print("running for galaxy field: %d" % (imid))
        # PSF
        if "%" in self.psf_fname:
            psf_fname = self.psf_fname % imid
        else:
            psf_fname = self.psf_fname
        psf_data2, psf_data3 = self.prepare_psf(
            psf_fname,
            self.rcut,
            self.image_nx,
        )

        # Simulate noise data
        if self.noi_var > 1e-20:
            print("Add noise with variance: %.4f" % self.noi_var)
            rng = np.random.RandomState(imid + 212)
            noise_data = rng.normal(
                scale=np.sqrt(self.noi_var), size=(self.image_ny, self.image_nx)
            )
        else:
            print("Do not add noise")
            noise_data = 0.0

        # FPFS Task
        # FPFS noise task
        if not os.path.isfile(self.ncov_fname) and self.noi_var > 1e-20:
            noise_task = fpfs.image.measure_noise_cov(
                psf_data2,
                sigma_arcsec=self.sigma_as,
                pix_scale=self.scale,
                sigma_detect=self.sigma_det,
            )
            cov_elem = noise_task.measure(self.noise_pow)
            pyfits.writeto(self.ncov_fname, np.array(cov_elem))

        # FPFS measurement task
        meas_task = fpfs.image.measure_source(
            psf_data2,
            sigma_arcsec=self.sigma_as,
            pix_scale=self.scale,
            sigma_detect=self.sigma_det,
        )
        print("The upper limit of wave number is %s pixels" % (meas_task.klim_pix))

        for ishear in self.pendList:
            print("FPFS measurement on simulation: %04d, %s" % (imid, ishear))
            gal_fname = os.path.join(
                self.imgdir,
                "image_%04d-%s.fits" % (imid, ishear),
            )
            if not os.path.isfile(gal_fname):
                print("Cannot find input galaxy file: %s" % gal_fname)
                return
            gal_data = pyfits.getdata(gal_fname) + noise_data
            assert gal_data.shape == (
                self.image_ny,
                self.image_nx,
            ), "The input image shape is different to the ini file"
            out_fname = os.path.join(
                self.catdir, "src_%05d-%s-rot_0.fits" % (imid, ishear)
            )
            if os.path.exists(out_fname):
                print("Already has measurement for this simulation.")
                continue

            cutmag = 26.0
            thres = 10 ** ((self.magz - cutmag) / 2.5) * self.scale**2.0
            thres2 = -thres / 20.0
            coords = fpfs.image.detect_sources(
                gal_data,
                psf_data3,
                gsigma=meas_task.sigmaF_det,
                thres=thres,
                thres2=thres2,
                klim=meas_task.klim,
            )
            print("pre-selected number of sources: %d" % len(coords))
            out = meas_task.measure(gal_data, coords)
            out = meas_task.get_results(out)
            pyfits.writeto(out_fname, out)
            del out, coords, gal_data, out_fname
            gc.collect()
        jax.clear_caches()
        jax.clear_backends()
        print("finish %s" % (imid))
        return

    def __call__(self, imid):
        print("start ID: %d" % (imid))
        return self.run(imid)


if __name__ == "__main__":
    parser = ArgumentParser(description="fpfs procsim")
    parser.add_argument("--minId", required=True, type=int, help="minimum ID, e.g. 0")
    parser.add_argument(
        "--maxId", required=True, type=int, help="maximum ID, e.g. 4000"
    )
    parser.add_argument("--config", required=True, type=str, help="configure file name")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--ncores",
        dest="n_cores",
        default=1,
        type=int,
        help="Number of processes (uses multiprocessing).",
    )
    group.add_argument(
        "--mpi",
        dest="mpi",
        default=False,
        action="store_true",
        help="Run with MPI.",
    )
    args = parser.parse_args()

    pool = schwimmbad.choose_pool(mpi=args.mpi, processes=args.n_cores)

    worker = Worker(args.config)
    refs = list(range(args.minId, args.maxId))
    for r in pool.map(worker, refs):
        pass
    pool.close()
