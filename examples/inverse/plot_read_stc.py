"""
===================
Reading an STC file
===================

STC files contain activations on cortex ie. source
reconstructions
"""
# Author: Alexandre Gramfort <alexandre.gramfort@telecom-paristech.fr>
#
# License: BSD (3-clause)

print(__doc__)

import mne
from mne.datasets import sample

data_path = sample.data_path()
fname = data_path + '/MEG/sample/sample_audvis-meg'

stc = mne.read_source_estimate(fname)

n_vertices, n_samples = stc.data.shape
print("stc data size: %s (nb of vertices) x %s (nb of samples)"
      % (n_vertices, n_samples))

# View source activations
import matplotlib.pyplot as plt
plt.plot(stc.times, stc.data[::100, :].T)
plt.xlabel('time (ms)')
plt.ylabel('Source amplitude')
plt.show()
