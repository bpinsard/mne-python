"""
================================================================
Compute sparse inverse solution based on L1/L2 mixed norm (MxNE)
================================================================

See
Gramfort A., Kowalski M. and Hamalainen, M,
Mixed-norm estimates for the M/EEG inverse problem using accelerated
gradient methods, Physics in Medicine and Biology, 2012
http://dx.doi.org/10.1088/0031-9155/57/7/1937
"""
# Author: Alexandre Gramfort <alexandre.gramfort@telecom-paristech.fr>
#
# License: BSD (3-clause)

print(__doc__)

import mne
from mne.datasets import sample
from mne.inverse_sparse import mixed_norm
from mne.minimum_norm import make_inverse_operator, apply_inverse
from mne.viz import plot_sparse_source_estimates

data_path = sample.data_path()
fwd_fname = data_path + '/MEG/sample/sample_audvis-meg-eeg-oct-6-fwd.fif'
ave_fname = data_path + '/MEG/sample/sample_audvis-ave.fif'
cov_fname = data_path + '/MEG/sample/sample_audvis-cov.fif'
subjects_dir = data_path + '/subjects'

# Read noise covariance matrix
cov = mne.read_cov(cov_fname)
# Handling average file
condition = 'Left Auditory'
evoked = mne.read_evokeds(ave_fname, condition=condition, baseline=(None, 0))
evoked.crop(tmin=0, tmax=0.3)
# Handling forward solution
forward = mne.read_forward_solution(fwd_fname, surf_ori=True)

cov = mne.cov.regularize(cov, evoked.info)

ylim = dict(eeg=[-10, 10], grad=[-400, 400], mag=[-600, 600])
evoked.plot(ylim=ylim, proj=True)

###############################################################################
# Run solver
alpha = 70  # regularization parameter between 0 and 100 (100 is high)
loose, depth = 0.2, 0.9  # loose orientation & depth weighting

# Compute dSPM solution to be used as weights in MxNE
inverse_operator = make_inverse_operator(evoked.info, forward, cov,
                                         loose=None, depth=depth, fixed=True)
stc_dspm = apply_inverse(evoked, inverse_operator, lambda2=1. / 9.,
                         method='dSPM')

# Compute MxNE inverse solution
stc, residual = mixed_norm(evoked, forward, cov, alpha, loose=loose,
                           depth=depth, maxit=3000, tol=1e-4,
                           active_set_size=10, debias=True, weights=stc_dspm,
                           weights_min=8., return_residual=True)

residual.plot(ylim=ylim, proj=True)

###############################################################################
# View in 2D and 3D ("glass" brain like 3D plot)
plot_sparse_source_estimates(forward['src'], stc, bgcolor=(1, 1, 1),
                             opacity=0.1, fig_name="MxNE (cond %s)" % condition)

# and on the fsaverage brain after morphing
stc_fsaverage = stc.morph(subject_from='sample', subject_to='fsaverage',
                          grade=None, sparse=True, subjects_dir=subjects_dir)
src_fsaverage_fname = subjects_dir + '/fsaverage/bem/fsaverage-ico-5-src.fif'
src_fsaverage = mne.read_source_spaces(src_fsaverage_fname)

plot_sparse_source_estimates(src_fsaverage, stc_fsaverage, bgcolor=(1, 1, 1),
                             opacity=0.1)
