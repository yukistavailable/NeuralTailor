"""
    Module for basic operations on patterns
    The code is compatible with both Python 2.7 (to be used in Maya 2020) and higher versions
"""
# Basic
from __future__ import print_function
from __future__ import division
import copy
import errno
import json
import numpy as np
import os
import random
import sys
if sys.version_info[0] >= 3:
    from scipy.spatial.transform import Rotation  # Not available in scipy 0.19.1 installed for Maya

standard_filenames = [
    'specification',  # e.g. used by dataset generation
    'template', 
    'prediction'
]

pattern_spec_template = {
    'pattern': {
        'panels': {},
        'stitches': []
    },
    'parameters': {},
    'parameter_order': [],
    'properties': {  # these are to be ensured when pattern content is updated directly
        'curvature_coords': 'relative', 
        'normalize_panel_translation': False, 
        'units_in_meter': 100  # cm
    }
}

panel_spec_template = {
    'translation': [ 0, 0, 0 ],
    'rotation': [ 0, 0, 0 ],
    'vertices': [],
    'edges': []
}



class BasicPattern(object):
    """Loading & serializing of a pattern specification in custom JSON format.
        Input:
            * Pattern template in custom JSON format
        Output representations: 
            * Pattern instance in custom JSON format 
                * In the current state
        
        Not implemented: 
            * Convertion to NN-friendly format
            * Support for patterns with darts
    """

    # ------------ Interface -------------

    def __init__(self, pattern_file=None):
        
        self.spec_file = pattern_file
        
        if pattern_file is not None: # load pattern from file
            self.path = os.path.dirname(pattern_file)
            self.name = BasicPattern.name_from_path(pattern_file)
            self.reloadJSON()
        else: # create empty pattern
            self.path = None
            self.name = self.__class__.__name__
            self.spec = copy.deepcopy(pattern_spec_template)
            self.pattern = self.spec['pattern']
            self.properties = self.spec['properties']  # mandatory part

    def reloadJSON(self):
        """(Re)loads pattern info from spec file. 
        Useful when spec is updated from outside"""
        if self.spec_file is None:
            print('BasicPattern::Warning::{}::Pattern is not connected to any file. Reloadig from file request ignored.'.format(
                self.name
            ))
            return

        with open(self.spec_file, 'r') as f_json:
            self.spec = json.load(f_json)
        self.pattern = self.spec['pattern']
        self.properties = self.spec['properties']  # mandatory part

        # template normalization - panel translations and curvature to relative coords
        self._normalize_template()

    def serialize(self, path, to_subfolder=True, tag=''):
        # log context
        if to_subfolder:
            log_dir = os.path.join(path, self.name)
            try:
                os.makedirs(log_dir)
            except OSError as e:
                if e.errno != errno.EEXIST:
                    raise
            spec_file = os.path.join(log_dir, tag + 'specification.json')
        else:
            log_dir = path
            spec_file = os.path.join(path, (self.name + tag + '_specification.json'))

        # Save specification
        with open(spec_file, 'w') as f_json:
            json.dump(self.spec, f_json, indent=2)
        
        return log_dir

    def is_self_intersecting(self):
        """returns True if any of the pattern panels are self-intersecting"""
        return any(map(self._is_panel_self_intersecting, self.pattern['panels']))

    @staticmethod
    def name_from_path(pattern_file):
        name = os.path.splitext(os.path.basename(pattern_file))[0]
        if name in standard_filenames:  # use name of directory instead
            path = os.path.dirname(pattern_file)
            name = os.path.basename(os.path.normpath(path))
        return name

    # --------- Special representations -----
    def pattern_as_tensors(self, pad_panels_to_len=None, with_placement=False):
        """Return pattern in format suitable for NN inputs/outputs
            * 3D tensor of panel edges
            * 3D tensor of panel's 3D translations
            * 3D tensor of panel's 3D rotations
            with_placement tag is given mostly for backward compatibility
            """
        if sys.version_info[0] < 3:
            raise RuntimeError('BasicPattern::Error::pattern_as_tensors() is only supported for Python 3.6+ and Scipy 1.2+')
        
        # get panel ordering
        panel_order = self.panel_order()

        # Calculate max edge count among panels -- if not provided
        panel_lens = [len(panel['edges']) for name, panel in self.pattern['panels'].items()]
        max_len = pad_panels_to_len if pad_panels_to_len is not None else max(panel_lens)

        panel_seqs = []
        panel_translations = []
        panel_rotations = []
        for panel_name in panel_order:
            edges, rot, transl = self.panel_as_numeric(panel_name, pad_to_len=max_len)
            panel_seqs.append(edges)
            panel_translations.append(transl)
            panel_rotations.append(rot)
        
        # format result as requested
        result = [np.stack(panel_seqs)]
        if with_placement:
            result.append(np.stack(panel_rotations))
            result.append(np.stack(panel_translations))

        return tuple(result) if len(result) > 1 else result[0]

    def pattern_from_tensors(self, pattern_representation, panel_rotations=None, panel_translations=None, padded=False):
        """Create panels from given panel representation. 
            Assuming that representation uses cm as units"""
        # TODO updating stitches?
        if sys.version_info[0] < 3:
            raise RuntimeError('BasicPattern::Error::pattern_from_tensors() is only supported for Python 3.6+ and Scipy 1.2+')

        # remove existing panels -- start anew
        self.pattern['panels'] = {}
        for idx in range(len(pattern_representation)):
            self.panel_from_numeric(
                'panel_' + str(idx), 
                pattern_representation[idx], 
                rotation_6=panel_rotations[idx] if panel_rotations is not None else None,
                translation=panel_translations[idx] if panel_translations is not None else None,
                padded=padded)

    def panel_as_numeric(self, panel_name, pad_to_len=None):
        """Represent panel as sequence of edges with each edge as vector of fixed length plus the info on panel placement.
            * Vertex coordinates are recalculated s.t. zero is at the low left corner; 
            * Edges are returned in additive manner: 
                each edge as a vector that needs to be added to previous edges to get a 2D coordinate of end vertex
            * Panel placement (translation & Rotation) is returned according to the shift needed for sequential representation
        """
        if sys.version_info[0] < 3:
            raise RuntimeError('BasicPattern::Error::panel_as_numeric() is only supported for Python 3.6+ and Scipy 1.2+')

        panel = self.pattern['panels'][panel_name]
        vertices = np.array(panel['vertices'])

        # ---- offset vertices to have one (~low-left) vertex at (0, 0) -- deterministically ----
        # bounding box low-left to origin
        left_corner = np.min(vertices, axis=0)
        shift = - left_corner
        vertices = vertices - left_corner

        # ids of verts sitting on Ox
        full_range = np.arange(vertices.shape[0])
        on_ox_ids = full_range[np.isclose(vertices[:, 1], 0)]

        # choose the one closest to x
        origin_candidate = np.argmin(vertices[on_ox_ids, :], axis=0)[0]  # only need min on x axis
        origin_id = on_ox_ids[origin_candidate]
        # Chosen vertex to origin
        shift = shift - vertices[origin_id]
        vertices = vertices - vertices[origin_id]

        # ----- Construct edge sequence ----------
        # Edge that starts at origin
        first_edge = [idx for idx, edge in enumerate(panel['edges']) if edge['endpoints'][0] == origin_id]
        first_edge = first_edge[0]

        # iterate over edges starting from the chosen origin
        # https://stackoverflow.com/questions/2150108/efficient-way-to-rotate-a-list-in-python
        rotated_edges = panel['edges'][first_edge:] + panel['edges'][:first_edge]
        edge_sequence = []
        for edge in rotated_edges:
            edge_sequence.append(self._edge_as_vector(vertices, edge))

        # padding if requested
        if pad_to_len is not None:
            if len(edge_sequence) > pad_to_len:
                raise ValueError('BasicPattern::{}::panel {} cannot fit into requested length: {} edges to fit into {}'.format(
                    self.name, panel_name, len(edge_sequence), pad_to_len))
            for _ in range(len(edge_sequence), pad_to_len):
                edge_sequence.append(np.zeros_like(edge_sequence[0]))
        
        # ----- 3D placement convertion  ------
        # Follows the Maya convention: intrinsic xyz Euler Angles
        # https://docs.scipy.org/doc/scipy/reference/generated/scipy.spatial.transform.Rotation.html
        panel_rotation = Rotation.from_euler('xyz', panel['rotation'], degrees=True)
        panel_rotation = panel_rotation.as_matrix()

        # Global Translation with compensative update for local origin change (shift)
        shift = np.append(shift, 0)  # translation to 3D
        comenpensating_shift = - panel_rotation.dot(shift)
        translation = np.array(panel['translation']) + comenpensating_shift

        rotation_representation = panel_rotation.flatten(order='F')[:6]  # first two columns
        return np.stack(edge_sequence, axis=0), rotation_representation, translation

    def panel_from_numeric(self, panel_name, edge_sequence, rotation_6=None, translation=None, padded=False):
        """ Updates or creates panel from NN-compatible numeric representation
            * Set panel vertex (local) positions & edge dictionaries from given edge sequence
            * Set panel 3D translation and orientation if given. Accepts 6-element rotation representation -- first two colomns of rotation matrix"""
        if sys.version_info[0] < 3:
            raise RuntimeError('BasicPattern::Error::panel_from_numeric() is only supported for Python 3.6+ and Scipy 1.2+')

        if panel_name not in self.pattern['panels']:
            # add new panel! =)
            self.pattern['panels'][panel_name] = copy.deepcopy(panel_spec_template)

        if padded:
            # edge sequence might be ending with pad values
            selection = ~np.all(np.isclose(edge_sequence, 0, atol=1.5), axis=1)  # only non-zero rows
            edge_sequence = edge_sequence[selection]

        # ---- Convert edge representation ----
        vertices = np.array([[0, 0]])  # first vertex is always at origin
        edges = []
        for idx in range(len(edge_sequence) - 1):
            edge_info = edge_sequence[idx]
            next_vert = vertices[idx] + edge_info[:2]
            vertices = np.vstack([vertices, next_vert])
            edges.append(self._edge_dict(idx, idx + 1, edge_info[2:4]))

        # last edge is a special case
        idx = len(vertices) - 1
        edge_info = edge_sequence[-1]
        fin_vert = vertices[-1] + edge_info[:2]
        if all(np.isclose(fin_vert, 0)):
            edges.append(self._edge_dict(idx, 0, edge_info[2:4]))
        else:
            # TODO raise RuntimeError('BasicPattern::Error::Edge sequence do not return to origin')
            print('BasicPattern::Warning::{} with panel {}::Edge sequence do not return to origin. '
                ' Creating extra vertex'.format(self.name, panel_name))
            vertices = np.vstack([vertices, fin_vert])
            edges.append(self._edge_dict(idx, idx + 1, edge_info[2:4]))

        # update panel itself
        panel = self.pattern['panels'][panel_name]
        panel['vertices'] = vertices.tolist()
        panel['edges'] = edges

        # ----- 3D placement setup --------
        if translation is not None:
            # simply set as is
            panel['translation'] = translation.tolist()
        
        if rotation_6 is not None:
            # convert from 6-elements representation
            col_1 = rotation_6[:3] / np.linalg.norm(rotation_6[:3])
            col_2 = rotation_6[3:] / np.linalg.norm(rotation_6[3:])
            col_3 = np.cross(col_1, col_2)
            rot_mat = np.column_stack((col_1, col_2, col_3))
            rotation = Rotation.from_matrix(rot_mat)
            # Follows the Maya convention: intrinsic xyz Euler Angles
            panel['rotation'] = rotation.as_euler('xyz', degrees=True).tolist()
        
        print('BasicPattern::Warning::{}::Edge and vertex info updated for panel {}. Stitches might be broken'.format(self.name, panel_name))

    def panel_order(self, name_list=None, dim=0, tolerance=5):
        """Ordering of the panels based on their 3D translation values.
            * Using cm as units for tolerance
            * Sorting X -> Y -> Z (left-right (looking from Z) then down-up then back-front)
            * based on the fuzzysort suggestion here https://stackoverflow.com/a/24024801/11206726"""

        if name_list is None:  # start from beginning
            name_list = self.pattern['panels'].keys() 
        # consider only translations of the requested panel names
        reference = [self.pattern['panels'][panel_n]['translation'][dim] for panel_n in name_list]
        sorted_couple = sorted(zip(reference, name_list))  # sorts according to the first list
        sorted_reference, sorted_names = zip(*sorted_couple)
        sorted_names = list(sorted_names)

        if (dim + 1) < 3:  # 3D is max
            # re-sort values by next dimention if they have similar values in current dimention
            fuzzy_start = 0
            for fuzzy_end in range(1, len(sorted_reference)):
                if sorted_reference[fuzzy_end] - sorted_reference[fuzzy_start] >= tolerance:
                    # the range of similar values is completed
                    if fuzzy_end - fuzzy_start > 1:
                        sorted_names[fuzzy_start:fuzzy_end] = self.panel_order(
                            sorted_names[fuzzy_start:fuzzy_end], dim + 1, tolerance)
                    fuzzy_start = fuzzy_end  # start counting similar values anew

            # take care of the tail
            if fuzzy_start != fuzzy_end:
                sorted_names[fuzzy_start:] = self.panel_order(
                    sorted_names[fuzzy_start:], dim + 1, tolerance)

        return sorted_names

    def _edge_as_vector(self, vertices, edge_dict):
        """Represent edge as vector of fixed length: 
            * First 2 elements: Vector endpoint. 
                Original edge endvertex positions can be restored if edge vector is added to the start point,
                which in turn could be obtained from previous edges in the panel loop
            * Next 2 elements: Curvature values 
                Given in relative coordinates. With zeros if edge is not curved 
        """
        edge_verts = vertices[edge_dict['endpoints']]
        edge_vector = edge_verts[1] - edge_verts[0]
        curvature = np.array(edge_dict['curvature']) if 'curvature' in edge_dict else [0, 0]

        return np.concatenate([edge_vector, curvature])

    def _edge_dict(self, vstart, vend, curvature):
        """Convert given info into the proper edge dictionary representation"""
        edge_dict = {'endpoints': [vstart, vend]}
        if not all(np.isclose(curvature, 0)):  # curvature part
            edge_dict['curvature'] = curvature.tolist()
        return edge_dict

    # --------- Pattern operations ----------
    def _normalize_template(self):
        """
        Updated template definition for convenient processing:
            * Converts curvature coordinates to realitive ones (in edge frame) -- for easy length scaling
            * snaps each panel center to (0, 0) if requested in props
            * scales everything to cm
        """
        if self.properties['curvature_coords'] == 'absolute':
            for panel in self.pattern['panels']:
                # convert curvature 
                vertices = self.pattern['panels'][panel]['vertices']
                edges = self.pattern['panels'][panel]['edges']
                for edge in edges:
                    if 'curvature' in edge:
                        edge['curvature'] = self._control_to_relative_coord(
                            vertices[edge['endpoints'][0]], 
                            vertices[edge['endpoints'][1]], 
                            edge['curvature']
                        )
            # now we have new property
            self.properties['curvature_coords'] = 'relative'
        
        if 'units_in_meter' in self.properties:
            if self.properties['units_in_meter'] != 100:
                for panel in self.pattern['panels']:
                    self._normalize_panel_scaling(panel, self.properties['units_in_meter'])
                # now we have cm
                self.properties['original_units_in_meter'] = self.properties['units_in_meter']
                self.properties['units_in_meter'] = 100
                print('Warning: pattern units converted to cm')
        else:
            print('Warning: units not specified in the pattern. Scaling normalization was not applied')

        # after curvature is converted!!
        # Only if requested
        if ('normalize_panel_translation' in self.properties 
                and self.properties['normalize_panel_translation']):
            print('Normalizing translation!')
            self.properties['normalize_panel_translation'] = False  # one-time use property. Preverts rotation issues on future reads
            for panel in self.pattern['panels']:
                # put origin in the middle of the panel-- 
                offset = self._normalize_panel_translation(panel)
                # udpate translation vector
                original = self.pattern['panels'][panel]['translation'] 
                self.pattern['panels'][panel]['translation'] = [
                    original[0] + offset[0], 
                    original[1] + offset[1], 
                    original[2], 
                ]

    def _normalize_panel_translation(self, panel_name):
        """ Convert panel vertices to local coordinates: 
            Shifts all panel vertices s.t. origin is at the center of the panel
        """
        panel = self.pattern['panels'][panel_name]
        vertices = np.asarray(panel['vertices'])
        offset = np.mean(vertices, axis=0)
        vertices = vertices - offset

        panel['vertices'] = vertices.tolist()

        return offset
    
    def _normalize_panel_scaling(self, panel_name, units_in_meter):
        """Convert all panel info to cm. I assume that curvature is alredy converted to relative coords -- scaling does not need update"""
        scaling = 100 / units_in_meter
        # vertices
        vertices = np.array(self.pattern['panels'][panel_name]['vertices'])
        vertices = scaling * vertices
        self.pattern['panels'][panel_name]['vertices'] = vertices.tolist()

        # translation
        translation = self.pattern['panels'][panel_name]['translation']
        self.pattern['panels'][panel_name]['translation'] = [scaling * coord for coord in translation]

    def _control_to_abs_coord(self, start, end, control_scale):
        """
        Derives absolute coordinates of Bezier control point given as an offset
        """
        edge = end - start
        edge_perp = np.array([-edge[1], edge[0]])

        control_start = start + control_scale[0] * edge
        control_point = control_start + control_scale[1] * edge_perp

        return control_point 
    
    def _control_to_relative_coord(self, start, end, control_point):
        """
        Derives relative (local) coordinates of Bezier control point given as 
        a absolute (world) coordinates
        """
        start, end, control_point = np.array(start), np.array(end), \
            np.array(control_point)

        control_scale = [None, None]
        edge_vec = end - start
        edge_len = np.linalg.norm(edge_vec)
        control_vec = control_point - start
        
        # X
        # project control_vec on edge_vec by dot product properties
        control_projected_len = edge_vec.dot(control_vec) / edge_len 
        control_scale[0] = control_projected_len / edge_len
        # Y
        control_projected = edge_vec * control_scale[0]
        vert_comp = control_vec - control_projected  
        control_scale[1] = np.linalg.norm(vert_comp) / edge_len
        # Distinguish left&right curvature
        control_scale[1] *= np.sign(np.cross(control_point, edge_vec))

        return control_scale 

    def _edge_length(self, panel, edge):
        panel = self.pattern['panels'][panel]
        v_id_start, v_id_end = tuple(panel['edges'][edge]['endpoints'])
        v_start, v_end = np.array(panel['vertices'][v_id_start]), \
            np.array(panel['vertices'][v_id_end])
        
        return np.linalg.norm(v_end - v_start)

    def _restore(self, backup_copy):
        """Restores spec structure from given backup copy 
            Makes a full copy of backup to avoid accidential corruption of backup
        """
        self.spec = copy.deepcopy(backup_copy)
        self.pattern = self.spec['pattern']
        self.properties = self.spec['properties']  # mandatory part

    # -------- Checks ------------
    def _is_panel_self_intersecting(self, panel_name):
        """Checks whatever a given panel contains intersecting edges
        """
        panel = self.pattern['panels'][panel_name]
        vertices = np.array(panel['vertices'])

        # construct edge list in coordinates
        edge_list = []
        for edge in panel['edges']:
            edge_ids = edge['endpoints']
            edge_coords = vertices[edge_ids]
            if 'curvature' in edge:
                curv_abs = self._control_to_abs_coord(edge_coords[0], edge_coords[1], edge['curvature'])
                # view curvy edge as two segments
                # NOTE this aproximation might lead to False positives in intersection tests
                edge_list.append([edge_coords[0], curv_abs])
                edge_list.append([curv_abs, edge_coords[1]])
            else:
                edge_list.append(edge_coords.tolist())

        # simple pairwise checks of edges
        # Follows discussion in  https://math.stackexchange.com/questions/80798/detecting-polygon-self-intersection 
        for i1 in range(0, len(edge_list)):
            for i2 in range(i1 + 1, len(edge_list)):
                if self._is_segm_intersecting(edge_list[i1], edge_list[i2]):
                    return True
        
        return False          
        
    def _is_segm_intersecting(self, segment1, segment2):
        """Checks wheter two segments intersect 
            in the points interior to both segments"""
        # https://algs4.cs.princeton.edu/91primitives/
        def ccw(start, end, point):
            """A test whether three points form counterclockwize angle (>0) 
            Returns (<0) if they form clockwize angle
            0 if collinear"""
            return (end[0] - start[0]) * (point[1] - start[1]) - (point[0] - start[0]) * (end[1] - start[1])

        # == 0 for edges sharing a vertex
        if (ccw(segment1[0], segment1[1], segment2[0]) * ccw(segment1[0], segment1[1], segment2[1]) >= 0
                or ccw(segment2[0], segment2[1], segment1[0]) * ccw(segment2[0], segment2[1], segment1[1]) >= 0):
            return False
        return True


class ParametrizedPattern(BasicPattern):
    """
        Extention to BasicPattern that can work with parametrized patterns
        Update pattern with new parameter values & randomize those parameters
    """
    def __init__(self, pattern_file=None):
        super(ParametrizedPattern, self).__init__(pattern_file)
        self.parameters = self.spec['parameters']

        self.parameter_defaults = {
            'length': 1,
            'additive_length': 0,
            'curve': 1
        }
        self.constraint_types = [
            'length_equality'
        ]

    def param_values_list(self):
        """Returns current values of all parameters as a list in the pattern defined parameter order"""
        value_list = []
        for parameter in self.spec['parameter_order']:
            value = self.parameters[parameter]['value']
            if isinstance(value, list):
                value_list += value
            else:
                value_list.append(value)
        return value_list

    def apply_param_list(self, values):
        """Apply given parameters supplied as a list of param_values_list() form"""

        self._restore_template(params_to_default=False)

        # set new values
        value_count = 0
        for parameter in self.spec['parameter_order']:
            last_value = self.parameters[parameter]['value']
            if isinstance(last_value, list):
                self.parameters[parameter]['value'] = [values[value_count + i] for i in range(len(last_value))]
                value_count += len(last_value)
            else:
                self.parameters[parameter]['value'] = values[value_count]
                value_count += 1
        
        self._update_pattern_by_param_values()

    def reloadJSON(self):
        """(Re)loads pattern info from spec file. 
        Useful when spec is updated from outside"""
        super(ParametrizedPattern, self).reloadJSON()

        self.parameters = self.spec['parameters']
        self._normalize_param_scaling()

    def _restore(self, backup_copy):
        """Restores spec structure from given backup copy 
            Makes a full copy of backup to avoid accidential corruption of backup
        """
        super(ParametrizedPattern, self)._restore(backup_copy)
        self.parameters = self.spec['parameters']
    
    # ------- Direct pattern update -------
    def pattern_from_tensors(self, pattern_representation, panel_rotations=None, panel_translations=None, padded=False):
        """When direct update is applied to parametrized pattern, 
            all the parameter settings become invalid"""
        super().pattern_from_tensors(pattern_representation, panel_rotations, panel_translations, padded)

        # Invalidate parameter & constraints values
        self._invalidate_all_values()

    def panel_from_numeric(self, panel_name, edge_sequence, rotation_6=None, translation=None, padded=False):
        """When direct update is applied to parametrized pattern panels, 
            all the parameter settings become invalid"""
        super().panel_from_numeric(panel_name, edge_sequence, rotation_6, translation, padded)

        # Invalidate parameter & constraints values
        self._invalidate_all_values()

    # ---------- Parameters operations --------

    def _normalize_param_scaling(self):
        """Convert additive parameters to cm units"""

        if 'original_units_in_meter' in self.properties:    # pattern was scaled
            scaling = 100 / self.properties['original_units_in_meter']
            for parameter in self.parameters:
                if self.parameters[parameter]['type'] == 'additive_length': 
                    self.parameters[parameter]['value'] = scaling * self.parameters[parameter]['value']
                    self.parameters[parameter]['range'] = [
                        scaling * elem for elem in self.parameters[parameter]['range']
                    ]

            # now we have cm everywhere -- no need to keep units info
            self.properties.pop('original_units_in_meter', None)

            print('Warning: Parameter units were converted to cm')

    def _update_pattern_by_param_values(self):
        """
        Recalculates vertex positions and edge curves according to current
        parameter values
        (!) Assumes that the current pattern is a template:
                with all the parameters equal to defaults!
        """
        for parameter in self.spec['parameter_order']:
            value = self.parameters[parameter]['value']
            param_type = self.parameters[parameter]['type']
            if param_type not in self.parameter_defaults:
                raise ValueError("Incorrect parameter type. Alowed are "
                                 + self.parameter_defaults.keys())

            for panel_influence in self.parameters[parameter]['influence']:
                for edge in panel_influence['edge_list']:
                    if param_type == 'length':
                        self._extend_edge(panel_influence['panel'], edge, value)
                    elif param_type == 'additive_length':
                        self._extend_edge(panel_influence['panel'], edge, value, multiplicative=False)
                    elif param_type == 'curve':
                        self._curve_edge(panel_influence['panel'], edge, value)
        # finally, ensure secified constraints are held
        self._apply_constraints()    

    def _restore_template(self, params_to_default=True):
        """Restore pattern to it's state with all parameters having default values
            Recalculate vertex positions, edge curvatures & snap values to 1
        """
        # Follow process backwards
        self._invert_constraints()

        for parameter in reversed(self.spec['parameter_order']):
            value = self.parameters[parameter]['value']
            param_type = self.parameters[parameter]['type']
            if param_type not in self.parameter_defaults:
                raise ValueError("Incorrect parameter type. Alowed are "
                                 + self.parameter_defaults.keys())

            for panel_influence in reversed(self.parameters[parameter]['influence']):
                for edge in reversed(panel_influence['edge_list']):
                    if param_type == 'length':
                        self._extend_edge(panel_influence['panel'], edge, self._invert_value(value))
                    elif param_type == 'additive_length':
                        self._extend_edge(panel_influence['panel'], edge, 
                                          self._invert_value(value, multiplicative=False), 
                                          multiplicative=False)
                    elif param_type == 'curve':
                        self._curve_edge(panel_influence['panel'], edge, self._invert_value(value))
            
            # restore defaults
            if params_to_default:
                if isinstance(value, list):
                    self.parameters[parameter]['value'] = [self.parameter_defaults[param_type] for _ in value]
                else:
                    self.parameters[parameter]['value'] = self.parameter_defaults[param_type]

    def _extend_edge(self, panel_name, edge_influence, value, multiplicative=True):
        """
            Shrinks/elongates a given edge or edge collection of a given panel. Applies equally
            to straight and curvy edges tnks to relative coordinates of curve controls
            Expects
                * each influenced edge to supply the elongatoin direction
                * scalar scaling_factor
            'multiplicative' parameter controls the type of extention:
                * if True, value is treated as a scaling factor of the edge or edge projection -- default
                * if False, value is added to the edge or edge projection
        """
        if isinstance(value, list):
            raise ValueError("Multiple scaling factors are not supported")

        verts_ids, verts_coords, target_line, _ = self._meta_edge(panel_name, edge_influence)

        # calc extention pivot
        if edge_influence['direction'] == 'end':
            fixed = verts_coords[0]  # start is fixed
        elif edge_influence['direction'] == 'start':
            fixed = verts_coords[-1]  # end is fixed
        elif edge_influence['direction'] == 'both':
            fixed = (verts_coords[0] + verts_coords[-1]) / 2
        else:
            raise RuntimeError('Unknown edge extention direction {}'.format(edge_influence['direction']))

        # move verts 
        # * along target line that sits on fixed point (correct sign & distance along the line)
        verts_projection = np.empty(verts_coords.shape)
        for i in range(verts_coords.shape[0]):
            verts_projection[i] = (verts_coords[i] - fixed).dot(target_line) * target_line

        if multiplicative:
            # * to match the scaled projection (correct point of application -- initial vertex position)
            new_verts = verts_coords - (1 - value) * verts_projection
        else:
            # * to match the added projection: 
            # still need projection to make sure the extention derection is corect relative to fixed point
            
            # normalize first
            for i in range(verts_coords.shape[0]):
                norm = np.linalg.norm(verts_projection[i])
                if not np.isclose(norm, 0):
                    verts_projection[i] /= norm

            # zero projections were not normalized -- they will zero-out the effect
            new_verts = verts_coords + value * verts_projection

        # update in the initial structure
        panel = self.pattern['panels'][panel_name]
        for ni, idx in enumerate(verts_ids):
            panel['vertices'][idx] = new_verts[ni].tolist()

    def _curve_edge(self, panel_name, edge, scaling_factor):
        """
            Updated the curvature of an edge accoding to scaling_factor.
            Can only be applied to edges with curvature information
            scaling_factor can be
                * scalar -- only the Y of control point is changed
                * 2-value list -- both coordinated of control are updated
        """
        panel = self.pattern['panels'][panel_name]
        if 'curvature' not in panel['edges'][edge]:
            raise ValueError('Applying curvature scaling to non-curvy edge '
                             + str(edge) + ' of ' + panel_name)
        control = panel['edges'][edge]['curvature']

        if isinstance(scaling_factor, list):
            control = [
                control[0] * scaling_factor[0],
                control[1] * scaling_factor[1]
            ]
        else:
            control[1] *= scaling_factor

        panel['edges'][edge]['curvature'] = control

    def _invert_value(self, value, multiplicative=True):
        """If value is a list, return a list with each value inverted.
            'multiplicative' parameter controls the type of inversion:
                * if True, returns multiplicative inverse (1/value) == default
                * if False, returns additive inverse (-value)
        """
        if multiplicative:
            if isinstance(value, list):
                if any(np.isclose(value, 0)):
                    raise ZeroDivisionError('Zero value encountered while restoring multiplicative parameter.')
                return map(lambda x: 1 / x, value)
            else:
                if np.isclose(value, 0):
                    raise ZeroDivisionError('Zero value encountered while restoring multiplicative parameter.')
                return 1 / value
        else:
            if isinstance(value, list):
                return map(lambda x: -x, value)
            else:
                return -value

    def _apply_constraints(self):
        """Change the pattern to adhere to constraints if given in pattern spec
            Assumes no zero-length edges exist"""
        if 'constraints' not in self.spec:
            return 

        for constraint_n in self.spec['constraints']:  # order preserved as it's a list
            constraint = self.spec['constraints'][constraint_n]
            constraint_type = constraint['type']
            if constraint_type not in self.constraint_types:
                raise ValueError("Incorrect constraint type. Alowed are "
                                 + self.constraint_types)

            if constraint_type == 'length_equality':
                # get all length of the affected (meta) edges
                target_len = []
                for panel_influence in constraint['influence']:
                    for edge in panel_influence['edge_list']:
                        # TODO constraints along a custom vector are not well tested
                        _, _, _, length = self._meta_edge(panel_influence['panel'], edge)
                        edge['length'] = length
                        target_len.append(length)
                if len(target_len) == 0:
                    return
                # target as mean of provided edges
                target_len = sum(target_len) / len(target_len)  

                # calculate scaling factor for every edge to match max length
                # & update edges with it
                for panel_influence in constraint['influence']:
                    for edge in panel_influence['edge_list']:
                        scaling = target_len / edge['length'] 
                        if not np.isclose(scaling, 1):
                            edge['value'] = scaling
                            self._extend_edge(panel_influence['panel'], edge, edge['value'])

    def _invert_constraints(self):
        """Restore pattern to the state before constraint was applied"""
        if 'constraints' not in self.spec:
            return 

        # follow the process backwards
        for constraint_n in reversed(self.spec['constraint_order']):  # order preserved as it's a list
            constraint = self.spec['constraints'][constraint_n]
            constraint_type = constraint['type']
            if constraint_type not in self.constraint_types:
                raise ValueError("Incorrect constraint type. Alowed are "
                                 + self.constraint_types)

            if constraint_type == 'length_equality':
                # update edges with invertes scaling factor
                for panel_influence in constraint['influence']:
                    for edge in panel_influence['edge_list']:
                        scaling = self._invert_value(edge['value'])
                        self._extend_edge(panel_influence['panel'], edge, scaling)
                        edge['value'] = 1

    def _meta_edge(self, panel_name, edge_influence):
        """Returns info for the given edge or meta-edge in inified form"""

        panel = self.pattern['panels'][panel_name]
        edge_ids = edge_influence['id']
        if isinstance(edge_ids, list):
            # meta-edge
            # get all vertices in order
            verts_ids = [panel['edges'][edge_ids[0]]['endpoints'][0]]  # start
            for edge_id in edge_ids:
                verts_ids.append(panel['edges'][edge_id]['endpoints'][1])  # end vertices
        else:
            # single edge
            verts_ids = panel['edges'][edge_ids]['endpoints']

        verts_coords = []
        for idx in verts_ids:
            verts_coords.append(panel['vertices'][idx])
        verts_coords = np.array(verts_coords)

        # extention line
        if 'along' in edge_influence:
            target_line = edge_influence['along']
        else:
            target_line = verts_coords[-1] - verts_coords[0] 

        if np.isclose(np.linalg.norm(target_line), 0):
            raise ZeroDivisionError('target line is zero ' + str(target_line))
        else:
            target_line /= np.linalg.norm(target_line)

        return verts_ids, verts_coords, target_line, target_line.dot(verts_coords[-1] - verts_coords[0])

    def _invalidate_all_values(self):
        """Sets all values of params & constraints to None if not set already
            Useful in direct updates of pattern panels"""

        updated_once = False
        for parameter in self.parameters:
            if self.parameters[parameter]['value'] is not None:
                self.parameters[parameter]['value'] = None
                updated_once = True
        
        if 'constraints' in self.spec:
            for constraint in self.spec['constraints']:
                for edge_collection in self.spec['constraints'][constraint]['influence']:
                    for edge in edge_collection['edge_list']:
                        if edge['value'] is not None: 
                            edge['value'] = None
                            updated_once = True
        if updated_once:
            # only display worning if some new invalidation happened
            print('ParametrizedPattern::Warning::Parameter (& constraints) values are invalidated')

    # ---------- Randomization -------------
    def _randomize_pattern(self):
        """Robustly randomize current pattern"""
        # restore template state before making any changes to parameters
        self._restore_template(params_to_default=False)

        spec_backup = copy.deepcopy(self.spec)
        self._randomize_parameters()
        self._update_pattern_by_param_values()
        for _ in range(100):  # upper bound on trials to avoid infinite loop
            if not self.is_self_intersecting():
                break

            print('Warning::Randomized pattern is self-intersecting. Re-try..')
            self._restore(spec_backup)
            # Try again
            self._randomize_parameters()
            self._update_pattern_by_param_values()

    def _new_value(self, param_range):
        """Random value within range given as an iteratable"""
        value = random.uniform(param_range[0], param_range[1])
        # prevent non-reversible zero values
        if abs(value) < 1e-2:
            value = 1e-2 * (-1 if value < 0 else 1)
        return value

    def _randomize_parameters(self):
        """
        Sets new random values for the pattern parameters
        Parameter type agnostic
        """
        for parameter in self.parameters:
            param_ranges = self.parameters[parameter]['range']

            # check if parameter has multiple values (=> multiple ranges) like for curves
            if isinstance(self.parameters[parameter]['value'], list): 
                values = []
                for param_range in param_ranges:
                    values.append(self._new_value(param_range))
                self.parameters[parameter]['value'] = values
            else:  # simple 1-value parameter
                self.parameters[parameter]['value'] = self._new_value(param_ranges)


# ---------- test -------------
if __name__ == "__main__":
    import customconfig
    # from pattern.wrappers import VisPattern

    system_config = customconfig.Properties('./system.json')
    base_path = system_config['output']
    pattern = BasicPattern(os.path.join(system_config['templates_path'], 'skirts', 'skirt_4_panels.json'))
    # pattern = BasicPattern(os.path.join(system_config['templates_path'], 'basic tee', 'tee.json'))
    # pattern = VisPattern()
    # empty_pattern = BasicPattern()
    print(pattern.panel_order())

    tensor, rot, transl = pattern.pattern_as_tensors(with_placement=True)
    panel_name = 'right'
    print(pattern.pattern['panels'][panel_name])
    # edges, rot, transl = pattern.panel_as_sequence(panel_name)
    # print(edges, rot, transl)

    # tensor[2][0][0] -= 10

    # # tensor = tensor[:-1]
    pattern.pattern_from_tensors(tensor, rot, transl, padded=True)
    # pattern.panel_from_sequence(panel_name, edges, rot, transl, padded=True)
    print(pattern.pattern['panels']['panel_0'])
    print(pattern.panel_order())

    # pattern.name += '_placement_upd_1'
    # pattern.serialize(system_config['output'], to_subfolder=True)