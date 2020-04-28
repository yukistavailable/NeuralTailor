"""
    Run the simulation af a dataset
    Note that it's Python 2.7 friendly
"""
from __future__ import print_function
import os

# My modules
import mayaqltools as mymaya
import customconfig
reload(mymaya)
reload(customconfig)


if __name__ == "__main__":
    system_config = customconfig.Properties('./system.json')  # Make sure it's in \Autodesk\MayaNNNN\
    path = system_config['templates_path']

    # ------ Dataset Example ------
    # dataset = 'zero_grav_skirt_maya_coords_200420-14-15-copy'
    # datapath = os.path.join(system_config['output'], dataset)
    # dataset_file = os.path.join(datapath, 'dataset_properties.json')
    # props = customconfig.Properties(dataset_file)
    # props.set_basic(
    #     body='f_smpl_templatex300.obj', 
    #     data_folder=dataset  # in case data properties are from other dataset/folder, update info
    # )  

    # mymaya.simulation.batch_sim(path, path, datapath, props, caching=False)
    # props.serialize(dataset_file)

    # ------ Example for single template generation ------
    path_example = os.path.join(system_config['output'], 'zero_grav_skirt_maya_coords_200420-14-15')
    # props = customconfig.Properties(path_example + '/dataset_properties.json', True)  
    props = customconfig.Properties('D:/GK-Pattern-Data-Gen/from_editor/materials_200427-16-19-46/sim_props.json', True)  
    props.set_basic(
        body='f_smpl_templatex300.obj',
        templates='template_skirt_maya_coords.json'
    )
    # TODO Give path to template directly
    mymaya.simulation.single_file_sim(path_example, path, props, caching=False)
    print(props)
