#  Copyright (C) 2012-2015  Hannes H Loeffler, Julien Michel
#
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 2 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software
#  Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
#
#  For full details of the license please see the COPYING file
#  that should have come with this distribution.

r"""
The morphing class maps one ligand into a second one thus creating a morph pair.

Requires a ligand in supplied two objects i.e. either of type Ligand or Complex.
"""


__revision__ = "$Id$"



import sys, os, re, shutil

from FESetup import const, errors, logger, report
#from FESetup.prepare.amber import gromacs
from . import util

import Sire.IO


REST_PDB_NAME = 'ligand_removed.pdb'
COMPAT_TABLE = {'Sire': 'pertfile', 'AMBER': 'sander/dummy',
                'AMBER/softcore': 'sander/softcore'}
WD_TABLE = {'pertfile': 'sire'}


class Morph(object):
    """The morphing class."""
    def __init__(self, initial, final, forcefield, FE_type = 'pertfile',
                 softcore_type = '', mcs_timeout = 60.0, mcs_sel = ''):
        """
        :param initial: the initial state of the morph pair
        :type initial: either Ligand or Complex
        :param final: the final state of the morph pair
        :type final: either Ligand or Complex
        :param forcefield: force field details
        :type forcefield: ForceField
        :param FE_type: the free energy type
        :type FE_type: str
        :param softcore_type: the softcore type
        :type softcore_type: str
        :raises: SetupError
        """

        try:
            FE_type = COMPAT_TABLE[FE_type]
        except KeyError:
            pass

        tmp_FE_type = FE_type.split('/')
        self.FE_type = tmp_FE_type[0]

        # FIXME: cleanup
        if self.FE_type == 'Sire':
            self.FE_type = 'pertfile'

        if len(tmp_FE_type) > 1:
            self.FE_sub_type = tmp_FE_type[1]
        else:
            self.FE_sub_type = ''

        self.sc_type = softcore_type

        self.topdir = os.getcwd()

        self.initial_dir = os.path.join(initial.topdir, initial.workdir,
                                        initial.mol_name)
        self.final_dir = os.path.join(final.topdir, final.workdir,
                                      final.mol_name)
        self.initial_name = initial.mol_name
        self.final_name = final.mol_name

        self.name = initial.mol_name + const.MORPH_SEP + final.mol_name

        try:
            type_dir = WD_TABLE[self.FE_type]
        except KeyError:
            type_dir = FE_type.replace('/', '-')

        self.dst = os.path.join(self.topdir, const.MORPH_WORKDIR, type_dir,
                                self.name)

        try:
            if not os.access(self.dst, os.F_OK):
                logger.write('Creating directory %s' % self.dst)
                os.makedirs(self.dst)
        except OSError as why:
            raise errors.SetupError(why)

        self.topol = None

        self.initial = initial
        self.final = final

        self.lig_morph = None
        self.frcmod = None
        self.frcmod0 = None
        self.frcmod1 = None

        self.ff = forcefield

        self.atoms_initial = None
        self.atoms_final = None
        self.lig_initial = None
        self.lig_final = None

        self.atom_map = None            # OrderedDict()
        self.reverse_atom_map = None    # OrderedDict()
        self.zz_atoms = []

        self.con_morph = None
        self.connect_final = None
        self.dummy_idx = []

        self.mcs_timeout = mcs_timeout
        self.mcs_sel = mcs_sel


    # context manager used to keep track of directory changes
    def __enter__(self):
        """Enter directory dst."""
        
        logger.write('Entering %s' % self.dst)
        os.chdir(self.dst)

        return self


    def __exit__(self, typ, value, traceback):
        """Leave directory dst and return to topdir."""

        logger.write('Entering %s\n' % self.topdir)
        os.chdir(self.topdir)

        return


    @report
    def setup(self, cmd1, cmd2):
        """
        Compute the atom mapping based on MCSS calculations.  Find dummy
        atoms. Set up parameters and connectivities for create_coord().  Create
        coordinates and topology for vacuum case.

        *Must* be first method called to properly setup Morph object.

        :raises: SetupError
        """

        initial_dir = os.path.join(self.topdir, const.LIGAND_WORKDIR,
                                   self.initial.mol_name)
        final_dir = os.path.join(self.topdir, const.LIGAND_WORKDIR,
                                 self.final.mol_name)

        system = 'vacuum'

        initial_top = os.path.join(initial_dir,
                                   system + self.initial.TOP_EXT)
        initial_crd = os.path.join(initial_dir,
                                   system + self.initial.RST_EXT)

        final_top = os.path.join(final_dir, system + self.final.TOP_EXT)
        final_crd = os.path.join(final_dir, system + self.final.RST_EXT)

        amber = Sire.IO.Amber()
        try:
            molecules_initial = amber.readCrdTop(initial_crd, initial_top)[0]
        except UserWarning as error:
            raise errors.SetupError('error opening %s/%s: %s' %
                                    (initial_crd, initial_top, error) )

        nmol_i = molecules_initial.molNums()
        nmol_i.sort()

        # we make the assumption that the ligand is the first mol in the
        # top/crd
        lig_initial = molecules_initial.at(nmol_i[0]).molecule()

        try:
            molecules_final = amber.readCrdTop(final_crd, final_top)[0]
        except UserWarning as error:
            raise errors.SetupError('error opening %s/%s: %s' %
                                    (final_crd, final_top, error) )

        nmol_f = molecules_final.molNums()
        nmol_f.sort()

        lig_final = molecules_final.at(nmol_f[0]).molecule()

        # user tagging mechanism as per feature request #1074
        lig0_isomap_file = os.path.join(self.topdir, self.initial.basedir,
                                        self.name + os.extsep + 'map')

        isotope_map = util.create_isotope_map(lig0_isomap_file)

        (lig_morph, self.atom_map, self.reverse_atom_map) = \
                    util.map_atoms(lig_initial, lig_final, self.mcs_timeout,
                                   isotope_map, self.mcs_sel)

        logger.write('\nAtom mapping between initial and final states:')

        for i, f in self.atom_map.items():
            logger.write("%s <--> %s" % (i.name, f.name) )

        logger.write('')

        self.dummy_idx = [inf.index for inf in self.atom_map if not inf.atom]

        atoms_initial = lig_initial.atoms()
        atoms_final = lig_final.atoms()

        lig_morph, con_morph, connect_final = \
                util.parm_conn(lig_morph, atoms_initial, lig_initial, lig_final,
                               self.atom_map, self.reverse_atom_map)


        lig_morph, lig_initial, lig_final, self.zz_atoms, = \
                util.dummy_coords(lig_morph, con_morph, atoms_initial,
                                  lig_initial, lig_final, self.atom_map,
                                  self.reverse_atom_map, connect_final,
                                  self.zz_atoms, self.dummy_idx)

        logger.write('\nWriting pert topology for %s%s\n' %
                     (self.FE_type, '/' + self.FE_sub_type if self.FE_sub_type
                      else '') )

        try:
            topol = __import__('topol.' + self.FE_type, globals(), locals(),
                               ['*'], -1)
        except ImportError as detail:
            sys.exit('Error: Unknown free energy type: %s' %
                     self.FE_type)
        except AttributeError as detail:
            sys.exit('Error: %s\nFailed to properly initialize %s' %
                     (detail, topol) )

        topol = topol.PertTopology(self.FE_sub_type, self.sc_type,
                                   self.ff, con_morph, atoms_initial,
                                   atoms_final, lig_initial, lig_final,
                                   self.atom_map, self.reverse_atom_map,
                                   self.zz_atoms)

        topol.setup(os.getcwd(), lig_morph, cmd1, cmd2)

        self.lig_morph = lig_morph
        self.lig_initial = lig_initial
        self.lig_final = lig_final

        self.atoms_initial = atoms_initial
        self.atoms_final = atoms_final

        self.con_morph = con_morph
        self.connect_final = connect_final

        self.topol = topol


    @report
    def create_coords(self, system, cmd1, cmd2, sys_rev = None):
        """
        Wrapper for the actual topology creation code.

        *Must* run after setup().

        :param system: must be either Ligand (solvated) or Complex (solvated).
           The ligand coordinates are computed while the coordinates of the rest
           of the system are taken from the unperturbed solvated system.
        :param cmd1: additional leap commands
        :param cmd2: additional leap commands
        :type system: either Ligand or Complex
        :type cmd1: string
        :type cmd2: string
        :type sys_rev: only Ligand at the moment passed in from dGrep.py
        """

        os.chdir(self.dst)        # FIXME: kludge to allow non-context use
        curr_dir = os.getcwd()

        if type(system) != self.ff.Complex and \
               type(system) != self.ff.Ligand:
            raise errors.SetupError('create_coord(): system must be '
                                    'either Ligand or Complex')

        dir_name = system.workdir

        if not os.access(dir_name, os.F_OK):
            os.mkdir(dir_name)

        os.chdir(dir_name)

        # FIXME: silly kludge to find top/crd because internal state not
        # available anymore
        top = os.path.join(system.dst, 'ionized.parm7')

        if not os.path.exists(top):
            top = os.path.join(system.dst, 'solvated.parm7')

        crd = util.search_crd(system)

        if not crd:
            raise errors.SetupError('no suitable rst7 file found')

        system.sander_rst = crd
        system.get_box_dims()

        try:
            mols = Sire.IO.Amber().readCrdTop(crd, top)[0]
        except UserWarning as error:
            raise errors.SetupError('error opening %s/%s: %s' %
                                    (crd, top, error) )

        lig, rest = util.split_system(mols)

        # FIXME: another kludge, working only for ligand but not complex
        if sys_rev:
            top2 = os.path.join(sys_rev.dst, 'ionized.parm7')

            if not os.path.exists(top2):
                top2 = os.path.join(sys_rev.dst, 'solvated.parm7')

            crd2 = util.search_crd(sys_rev)

            natoms = []
            boxdims = []

            for inpcrd in crd, crd2:
                with open(inpcrd, 'rb') as inp:
                    for ln, line in enumerate(inp):
                        if ln == 1:
                            natoms.append(line.split()[0])

                        # FIXME: replace with read(), assumes box data is present
                        dims = line

                boxdims.append(dims)

            if natoms[1] > natoms[0]:
                try:
                    mols2 = Sire.IO.Amber().readCrdTop(crd2, top2)[0]
                except UserWarning as error:
                    raise errors.SetupError('error opening %s/%s: %s' %
                                            (crd2, top2, error) )

                with open(const.BOX_DIMS, 'w') as boxfile:
                    boxfile.write(boxdims[1])

                rest = util.split_system(mols2)[1]
                crd = crd2


        if lig.nAtoms() != (len(self.atom_map) - len(self.dummy_idx) ):
            raise errors.SetupError('reference state has wrong number of '
                                    'atoms')

        logger.write('Using %s for coordinate file creation' % crd)

        atoms_initial = lig.atoms()

        # not using
        # Sire.IO.PDB().write(rest, REST_PDB_NAME)
        # because out of order and creates CONECTs for atoms > 99999
        # also: we can't use const.NOT_FIRST_PDB because leap has the strange
        # habit of centering a file saved with "savepdb" such that the origin
        # is in the center contrary to the prmtop which has it in one box
        # corner unless "set default nocenter on" is used (and coordinates
        # stay unmodified)
        with open(REST_PDB_NAME, 'w') as pdb:
            moln = rest.molNums()
            moln.sort()

            serial = 0
            resSeq = 0

            pdb.write('REMARK   Created with FESetup\n')

            # pseudo-PDB for leap
            for i in moln:
                mol = rest.at(i).molecule()
                ridx_old = -9999

                for atom in mol.atoms():
                    serial = (serial % 99999) + 1
                    atom_name = str(atom.name().value() )
                    resName =  atom.residue().name().value()
                    ridx = atom.residue().index().value()
                    coords = atom.property('coordinates')  # Math.Vector
                    x = coords.x()
                    y = coords.y()
                    z = coords.z()
                    elem = str(atom.property('element').symbol() )

                    if len(atom_name) < 4:
                        atom_name = ' %-3s' % atom_name

                    if ridx != ridx_old:
                        resSeq = (resSeq % 9999) + 1
                        ridx_old = ridx

                    pdb.write('ATOM  %5i %4s %-3s  %4i    %8.3f%8.3f%8.3f'
                              '                      %2s\n' %
                              (serial, atom_name, resName, resSeq, x, y, z,
                               elem) )

                pdb.write('TER\n')

            pdb.write('END\n')


        self.lig_morph = self.lig_morph.edit()

        # update coordinates only, everything else done already in setup()
        for iinfo in self.atom_map:
            if iinfo.atom:
                try:
                    base = atoms_initial.select(iinfo.index)
                except UserWarning as error:     # could be "anything"...
                    raise errors.SetupError('%s not found in reference: %s'
                                            % (iinfo.index, error) )

                coordinates = base.property('coordinates')

            new = self.lig_morph.atom(iinfo.index)
            new.setProperty('coordinates', coordinates)

            self.lig_morph = new.molecule()

        self.lig_morph.commit()

        self.lig_morph, self.lig_initial, self.lig_final, self.zz_atoms = \
                util.dummy_coords(self.lig_morph, self.con_morph,
                                  atoms_initial, self.lig_initial,
                                  self.lig_final, self.atom_map,
                                  self.reverse_atom_map, self.connect_final,
                                  self.zz_atoms, self.dummy_idx)

        self.topol.create_coords(curr_dir, dir_name, self.lig_morph,
                                 REST_PDB_NAME, system, cmd1, cmd2)

        os.chdir(curr_dir)

