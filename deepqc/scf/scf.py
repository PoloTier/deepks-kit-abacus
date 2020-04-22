import time
import torch
import numpy as np
from pyscf import lib
from pyscf.lib import logger
from pyscf import gto
from pyscf import scf
from deepqc.train.model import QCNet


_zeta = 1.5**np.array([17,13,10,7,5,3,2,1,0,-1,-2,-3])
_coef = np.diag(np.ones(_zeta.size)) - np.diag(np.ones(_zeta.size-1), k=1)
_table = np.concatenate([_zeta.reshape(-1,1), _coef], axis=1)
DEFAULT_BASIS = [[0, *_table.tolist()], [1, *_table.tolist()], [2, *_table.tolist()]]

DEVICE = 'cpu'#torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


class DeepSCF(scf.hf.RHF):
    # all variables and functions start with "t_" are torch related.
    # all variables and functions ends with "0" are original Hartree-Fock results
    # convention in einsum:
    #   i,j: orbital
    #   a,b: atom
    #   p,q: projected basis on atom
    #   r,s: mol basis in pyscf
    """Self Consistant Field solver for given QC model"""
    
    def __init__(self, mol, model, basis=None, device=DEVICE):
        super().__init__(mol)
        self.device = device
        if isinstance(model, str):
            model = QCNet.load(model).double().to(self.device)
        self.net = model

        # must be a list here, follow pyscf convention
        self.pbas = basis if basis is not None else DEFAULT_BASIS
        # a virtual molecule to be projected on
        self.pmol = gen_proj_mol(mol, self.pbas)
        # [1,1,1,...,3,3,3,...,5,5,5,...]
        self.shell_sec = sum(([2*b[0]+1] * (len(b)-1) for b in self.pbas), [])
        # < mol_ao | alpha^I_rlm >, shape=[nao x natom x nproj]
        self.t_proj_ovlp = torch.from_numpy(self.proj_ovlp()).double().to(self.device)
        # split the projected coeffs by shell (different r and l)
        self.t_ovlp_shells = torch.split(self.t_proj_ovlp, self.shell_sec, -1)
        # < alpha^I_rlm | mol_ao >< mol_ao | aplha^I_rlm' >
        # self.t_proj_ops = [torch.einsum('rap,saq->rsapq', po, po) 
        #                      for po in self.t_ovlp_shells]

        self.get_veff0 = super().get_veff
        self.nuc_grad_method0 = super().nuc_grad_method
        self._keys.update(self.__dict__.keys())

    def energy_elec0(self, dm=None, h1e=None, vhf=None):
        if vhf is None: vhf = self.get_veff0(dm=dm)
        return super().energy_elec(dm, h1e, vhf)
    
    def energy_tot0(self, dm=None, h1e=None, vhf=None):
        return self.energy_elec0(dm, h1e, vhf)[0] + self.energy_nuc()

    def get_veff(self, mol=None, dm=None, dm_last=0, vhf_last=0, hermi=1):
        """Hartree Fock potential + effective correlation potential"""
        if mol is None: 
            mol = self.mol
        if dm is None: 
            dm = self.make_rdm1()
        tic = (time.clock(), time.time())
        assert isinstance(dm, np.ndarray) and dm.ndim == 2
        
        # Hartree fock part
        v0_last = getattr(vhf_last, 'v0', 0)
        v0 = self.get_veff0(mol, dm, dm_last, v0_last, hermi)
        tic = logger.timer(self, 'v0', *tic)
        # Correlation part
        ec,vc = self.get_ec(dm)
        tic = logger.timer(self, 'vc', *tic)

        vtot = v0 + vc
        vtot = lib.tag_array(vtot, ec=ec, v0=v0)
        return vtot

    def energy_elec(self, dm=None, h1e=None, vhf=None):
        """return electronic energy and the 2-electron part contribution"""
        if dm is None: 
            dm = self.make_rdm1()
        if h1e is None: 
            h1e = self.get_hcore()
        if vhf is None or getattr(vhf, 'ec', None) is None: 
            vhf = self.get_veff(dm=dm)
        ec = vhf.ec
        e1 = np.einsum('ij,ji', h1e, dm)
        e_coul = np.einsum('ij,ji', vhf.v0, dm) * .5
        logger.debug(self, f'E1 = {e1}  Ecoul = {e_coul}  Ec = {ec}')
        return (e1+e_coul+ec).real, e_coul+ec

    def get_ec(self, dm=None):
        """return ec and vc corresponding to ec"""
        if dm is None:
            dm = self.make_rdm1()
        t_dm = torch.from_numpy(dm).double().to(self.device)
        t_ec, t_vc = self.t_get_ec(t_dm)
        return t_ec.item(), t_vc.detach().cpu().numpy()

    def t_get_ec(self, t_dm):
        """return ec and vc, all inputs and outputs are pytorch tensor"""
        # (D^I_rl)_mm' = \sum_i < alpha^I_rlm | phi_i >< phi_i | aplha^I_rlm' >
        proj_dms = [torch.einsum('rap,rs,saq->apq', po, t_dm, po).requires_grad_(True)
                        for po in self.t_ovlp_shells]
        proj_eigs = [torch.symeig(dm, eigenvectors=True)[0]
                        for dm in proj_dms]
        ceig = torch.cat(proj_eigs, dim=-1).unsqueeze(0) # 1 x natoms x nproj
        ec = self.net(ceig)
        grad_dms = torch.autograd.grad(ec, proj_dms)
        shell_vcs = [torch.einsum('rap,apq,saq->rs', po, gdm, po)
                        for po, gdm in zip(self.t_ovlp_shells, grad_dms)]
        vc = torch.stack(shell_vcs, 0).sum(0)
        return ec, vc

    def make_proj_rdms(self, dm=None, flatten=False):
        """return projected density matrix by shell"""
        if dm is None:
            dm = self.make_rdm1()
        ovlp_shell = [po.detach().cpu().numpy() 
                        for po in self.t_ovlp_shells]
        # [natoms x nsph x nsph] list
        proj_dms = [np.einsum('rap,rs,saq->apq', po, dm, po)
                        for po in ovlp_shell]
        if not flatten:
            return proj_dms
        else:
            return np.concatenate([s.reshape(*s.shape[:-2], -1) for s in proj_dms], axis=-1) 

    def make_eig(self, dm=None):
        """return eigenvalues of projected density matrix"""
        proj_dms = self.make_proj_rdms(dm, flatten=False)
        proj_eigs = [np.linalg.eigvalsh(dm) for dm in proj_dms]
        eig = np.concatenate(proj_eigs, -1) # natoms x nproj
        return eig

    def proj_intor(self, intor):
        """1-electron integrals between origin and projected basis"""
        proj = gto.intor_cross(intor, self.mol, self.pmol) 
        return proj
        
    def proj_ovlp(self):
        """overlap between origin and projected basis, reshaped"""
        nao = self.mol.nao
        natm = self.mol.natm
        pnao = self.pmol.nao
        proj = self.proj_intor("int1e_ovlp")
        # return shape [nao x natom x nproj]
        return proj.reshape(nao, natm, pnao // natm)

    def nuc_grad_method(self):
        from deepqc.scf.grad import Gradients
        return Gradients(self)


def gen_proj_mol(mol, basis) :
    natm = mol.natm
    nao = mol.nao
    mole_coords = mol.atom_coords(unit="Ang")
    test_mol = gto.Mole()
    test_mol.atom = [["Ne", coord] for coord in mole_coords]
    test_mol.basis = basis
    test_mol.build(0,0,unit="Ang")
    return test_mol


# if __name__ == '__main__':
#     mol = gto.Mole()
#     mol.verbose = 5
#     mol.output = None
#     mol.atom = [['He', (0, 0, 0)], ]
#     mol.basis = 'ccpvdz'
#     mol.build(0, 0)

#     def test_model(eigs):
#         assert eigs.shape[-1] == _zeta.size * 9
#         return 1e-3 * torch.sum(eigs, axis=(1,2))
    
#     # SCF Procedure
#     dscf = DeepSCF(mol, test_model)
#     energy = dscf.kernel()
#     print(energy)
