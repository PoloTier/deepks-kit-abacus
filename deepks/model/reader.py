import os,time,sys
import numpy as np
import torch


def concat_batch(tdicts, dim=0):
    keys = tdicts[0].keys()
    assert all(d.keys() == keys for d in tdicts)
    return {
        k: torch.cat([d[k] for d in tdicts], dim) 
        for k in keys
    }

def split_batch(tdict, size, dim=0):
    dsplit = {k: torch.split(v, size, dim) for k,v in tdict.items()}
    nsecs = [len(v) for v in dsplit.values()]
    assert all(ns == nsecs[0] for ns in nsecs)
    return [
        {k: v[i] for k, v in dsplit.items()}
        for i in range(nsecs[0])
    ]


class Reader(object):
    def __init__(self, data_path, batch_size, 
                 e_name="l_e_delta", d_name="dm_eig", 
                 f_name="l_f_delta", gvx_name="grad_vx", 
                 eg_name="eg_base", gveg_name="grad_veg", 
                 gldv_name="grad_ldv", conv_name="conv", **kwargs):
        self.data_path = data_path
        self.batch_size = batch_size
        self.e_path = self.check_exist(e_name+".npy")
        self.f_path = self.check_exist(f_name+".npy")
        self.d_path = self.check_exist(d_name+".npy")
        self.gvx_path = self.check_exist(gvx_name+".npy")
        self.eg_path = self.check_exist(eg_name+".npy")
        self.gveg_path = self.check_exist(gveg_name+".npy")
        self.gldv_path = self.check_exist(gldv_name+".npy")
        self.c_path = self.check_exist(conv_name+".npy")
        # load data
        self.load_meta()
        self.prepare()
        # initialize sample index queue
        self.idx_queue = []

    def check_exist(self, fname):
        if fname is None:
            return None
        fpath = os.path.join(self.data_path, fname)
        if os.path.exists(fpath):
            return fpath

    def load_meta(self):
        try:
            sys_meta = np.loadtxt(self.check_exist('system.raw'), dtype = int).reshape([-1])
            self.natm = sys_meta[0]
            self.nproj = sys_meta[-1]
        except:
            print('#', self.data_path, f"no system.raw, infer meta from data", file=sys.stderr)
            sys_shape = np.load(self.d_path).shape
            assert len(sys_shape) == 3, \
                f"descriptor has to be an order-3 array with shape [nframes, natom, nproj]"
            self.natm = sys_shape[1]
            self.nproj = sys_shape[2]
        self.ndesc = self.nproj

    def prepare(self):
        # load energy and check nframes
        data_ec = np.load(self.e_path).reshape([-1, 1])
        raw_nframes = data_ec.shape[0]
        data_dm = np.load(self.d_path)\
                    .reshape([raw_nframes, self.natm, self.ndesc])
        if self.c_path is not None:
            conv = np.load(self.c_path).reshape(raw_nframes)
        else:
            conv = np.ones(raw_nframes, dtype=bool)
        self.data_ec = data_ec[conv]
        self.data_dm = data_dm[conv]
        self.nframes = conv.sum()
        if self.nframes < self.batch_size:
            self.batch_size = self.nframes
            print('#', self.data_path, 
                 f"reset batch size to {self.batch_size}", file=sys.stderr)
        # load data in torch
        self.t_data = {}
        self.t_data["lb_e"] = torch.tensor(self.data_ec)
        self.t_data["eig"] = torch.tensor(self.data_dm)
        if self.f_path is not None and self.gvx_path is not None:
            self.t_data["lb_f"] = torch.tensor(
                np.load(self.f_path)\
                  .reshape(raw_nframes, self.natm, 3)[conv])
            self.t_data["gvx"] = torch.tensor(
                np.load(self.gvx_path)\
                  .reshape(raw_nframes, self.natm, 3, self.natm, self.ndesc)[conv])
        if self.eg_path is not None and self.gveg_path is not None:
            self.t_data['eg0'] = torch.tensor(
                np.load(self.eg_path)\
                  .reshape(raw_nframes, -1)[conv])
            self.t_data["gveg"] = torch.tensor(
                np.load(self.gveg_path)\
                  .reshape(raw_nframes, self.natm, self.ndesc, -1)[conv])
            self.neg = self.t_data['eg0'].shape[-1]
        if self.gldv_path is not None:
            self.t_data["gldv"] = torch.tensor(
                np.load(self.gldv_path)\
                  .reshape(raw_nframes, self.natm, self.ndesc)[conv])

    def sample_train(self):
        if self.batch_size == self.nframes == 1:
            return self.sample_all()
        if len(self.idx_queue) < self.batch_size:
            self.idx_queue = np.random.choice(self.nframes, self.nframes, replace=False)
        sample_idx = self.idx_queue[:self.batch_size]
        self.idx_queue = self.idx_queue[self.batch_size:]
        return {k: v[sample_idx] for k, v in self.t_data.items()}

    def sample_all(self):
        return self.t_data

    def get_train_size(self):
        return self.nframes

    def get_batch_size(self):
        return self.batch_size

    def get_nframes(self):
        return self.nframes


class GroupReader(object) :
    def __init__ (self, path_list, batch_size=1, group_batch=1, extra_label=True, **kwargs):
        if isinstance(path_list, str):
            path_list = [path_list]
        self.path_list = path_list
        self.batch_size = batch_size
        # init system readers
        Reader_class = (Reader if extra_label 
            and isinstance(kwargs.get('d_name', "dm_eig"), str) 
            else SimpleReader)
        self.readers = []
        self.nframes = []
        for ipath in self.path_list :
            ireader = Reader_class(ipath, batch_size, **kwargs)
            if ireader.get_nframes() == 0:
                print('# ignore empty dataset:', ipath, file=sys.stderr)
                continue
            self.readers.append(ireader)
            self.nframes.append(ireader.get_nframes())
        if not self.readers:
            raise RuntimeError("No system is avaliable")
        self.nsystems = len(self.readers)
        data_keys = self.readers[0].sample_all().keys()
        print(f"# load {self.nsystems} systems with fields {set(data_keys)}")
        # probability of each system
        self.ndesc = self.readers[0].ndesc
        self.sys_prob = [float(ii) for ii in self.nframes] / np.sum(self.nframes)
        
        self.group_batch = max(group_batch, 1)
        if self.group_batch > 1:
            self.group_dict = {}
            # self.group_index = {}
            for idx, r in enumerate(self.readers):
                shape = (r.natm, getattr(r, "neg", None))
                if shape in self.group_dict:
                    self.group_dict[shape].append(r)
                    # self.group_index[shape].append(idx)
                else:
                    self.group_dict[shape] = [r]
                    # self.group_index[shape] = [idx]
            self.group_prob = {n: sum(r.nframes for r in r_list) / sum(self.nframes)
                                for n, r_list in self.group_dict.items()}
            self.batch_prob_raw = {n: [r.nframes / r.batch_size for r in r_list] 
                                for n, r_list in self.group_dict.items()}
            self.batch_prob = {n: p / np.sum(p) for n, p in self.batch_prob_raw.items()}

        self._sample_used = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self._sample_used > self.get_train_size():
            self._sample_used = 0
            raise StopIteration
        sample = self.sample_train() if self.group_batch == 1 else self.sample_train_group()
        self._sample_used += sample["lb_e"].shape[0]
        return sample

    def sample_idx(self) :
        return np.random.choice(np.arange(self.nsystems), p=self.sys_prob)
        
    def sample_train(self, idx=None) :
        if idx is None:
            idx = self.sample_idx()
        return \
            self.readers[idx].sample_train()

    def sample_train_group(self):
        cidx = np.random.choice(len(self.group_prob), p=list(self.group_prob.values()))
        cshape = list(self.group_prob.keys())[cidx]
        cgrp = self.group_dict[cshape]
        csys = np.random.choice(cgrp, self.group_batch, p=self.batch_prob[cshape])
        batch = concat_batch([s.sample_train() for s in csys], dim=0)
        return batch

    def sample_all(self, idx=None) :
        if idx is None:
            idx = self.sample_idx()
        return \
            self.readers[idx].sample_all()
    
    def sample_all_batch(self, idx=None):
        if idx is not None:
            all_data = self.sample_all(idx)
            size = self.batch_size * self.group_batch
            yield from split_batch(all_data, size, dim=0)
        else:
            for i in range(self.nsystems):
                yield from self.sample_all_batch(i)

    def get_train_size(self) :
        return np.sum(self.nframes)

    def get_batch_size(self) :
        return self.batch_size

    def compute_data_stat(self, symm_sections=None):
        all_dm = np.concatenate([r.data_dm.reshape(-1,r.ndesc) for r in self.readers])
        if symm_sections is None:
            all_mean, all_std = all_dm.mean(0), all_dm.std(0)
        else:
            assert sum(symm_sections) == all_dm.shape[-1]
            dm_shells = np.split(all_dm, np.cumsum(symm_sections)[:-1], axis=-1)
            mean_shells = [d.mean().repeat(s) for d, s in zip(dm_shells, symm_sections)]
            std_shells = [d.std().repeat(s) for d, s in zip(dm_shells, symm_sections)]
            all_mean = np.concatenate(mean_shells, axis=-1)
            all_std = np.concatenate(std_shells, axis=-1)
        return all_mean, all_std

    def compute_prefitting(self, shift=None, scale=None, ridge_alpha=1e-8, symm_sections=None):
        if shift is None or scale is None:
            all_mean, all_std = self.compute_data_stat(symm_sections=symm_sections)
            if shift is None:
                shift = all_mean
            if scale is None:
                scale = all_std
        all_sdm = np.concatenate([((r.data_dm - shift) / scale).sum(1) for r in self.readers])
        all_natm = np.concatenate([[float(r.data_dm.shape[1])]*r.data_dm.shape[0] for r in self.readers])
        if symm_sections is not None: # in this case ridge alpha cannot be 0
            assert sum(symm_sections) == all_sdm.shape[-1]
            sdm_shells = np.split(all_sdm, np.cumsum(symm_sections)[:-1], axis=-1)
            all_sdm = np.stack([d.sum(-1) for d in sdm_shells], axis=-1)
        # build feature matrix
        X = np.concatenate([all_sdm, all_natm.reshape(-1,1)], -1)
        y = np.concatenate([r.data_ec for r in self.readers])
        I = np.identity(X.shape[1])
        I[-1,-1] = 0 # do not punish the bias term
        # solve ridge reg
        coef = np.linalg.solve(X.T @ X + ridge_alpha * I, X.T @ y).reshape(-1)
        weight, bias = coef[:-1], coef[-1]
        if symm_sections is not None:
            weight = np.concatenate([w.repeat(s) for w, s in zip(weight, symm_sections)], axis=-1)
        return weight, bias


class SimpleReader(object):
    def __init__(self, data_path, batch_size, 
                 e_name="l_e_delta", d_name="dm_eig", 
                 conv_filter=True, conv_name="conv", **kwargs):
        # copy from config
        self.data_path = data_path
        self.batch_size = batch_size
        self.e_name = e_name
        self.d_name = d_name if isinstance(d_name, (list, tuple)) else [d_name]
        self.c_filter = conv_filter
        self.c_name = conv_name
        self.load_meta()
        self.prepare()

    def load_meta(self):
        try:
            sys_meta = np.loadtxt(os.path.join(self.data_path,'system.raw'), dtype = int).reshape([-1])
            self.natm = sys_meta[0]
            self.nproj = sys_meta[-1]
        except:
            print('#', self.data_path, f"no system.raw, infer meta from data", file=sys.stderr)
            sys_shape = np.load(os.path.join(self.data_path, f'{self.d_name[0]}.npy')).shape
            assert len(sys_shape) == 3, \
                f"{self.d_name[0]} has to be an order-3 array with shape [nframes, natom, nproj]"
            self.natm = sys_shape[1]
            self.nproj = sys_shape[2]
    
    def prepare(self):
        self.index_count_all = 0
        data_ec = np.load(os.path.join(self.data_path,f'{self.e_name}.npy')).reshape([-1, 1])
        raw_nframes = data_ec.shape[0]
        data_dm = np.concatenate(
            [np.load(os.path.join(self.data_path,f'{dn}.npy'))\
               .reshape([raw_nframes, self.natm, -1])
            for dn in self.d_name], 
            axis=-1)
        if self.c_filter:
            conv = np.load(os.path.join(self.data_path,f'{self.c_name}.npy')).reshape(raw_nframes)
        else:
            conv = np.ones(raw_nframes, dtype=bool)
        self.data_ec = data_ec[conv]
        self.data_dm = data_dm[conv]
        self.nframes = conv.sum()
        self.ndesc = self.data_dm.shape[-1]
        # print(np.shape(self.inputs_train))
        if self.nframes < self.batch_size:
            self.batch_size = self.nframes
            print('#', self.data_path, f"reset batch size to {self.batch_size}", file=sys.stderr)
    
    def sample_train(self):
        if self.nframes == self.batch_size == 1:
            return self.sample_all()
        self.index_count_all += self.batch_size
        if self.index_count_all > self.nframes:
            # shuffle the data
            self.index_count_all = self.batch_size
            ind = np.random.choice(self.nframes, self.nframes, replace=False)
            self.data_ec = self.data_ec[ind]
            self.data_dm = self.data_dm[ind]
        ind = np.arange(self.index_count_all - self.batch_size, self.index_count_all)
        return {
            "lb_e": torch.from_numpy(self.data_ec[ind]), 
            "eig": torch.from_numpy(self.data_dm[ind])
        }

    def sample_all(self) :
        return {
            "lb_e": torch.from_numpy(self.data_ec), 
            "eig": torch.from_numpy(self.data_dm)
        }

    def get_train_size(self) :
        return self.nframes

    def get_batch_size(self) :
        return self.batch_size

    def get_nframes(self) :
        return self.nframes