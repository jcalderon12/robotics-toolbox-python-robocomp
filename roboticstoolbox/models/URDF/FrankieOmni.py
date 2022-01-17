#!/usr/bin/env python

import numpy as np
from roboticstoolbox.robot.ERobot import ERobot
from roboticstoolbox.robot.ELink import ELink
from roboticstoolbox.robot.ETS import ETS
from spatialmath import SE3


class FrankieOmni(ERobot):
    """
    Class that imports an Omnidirectional Frankie URDF model

    ``FrankieOmni()`` is a class which imports a FrankieOmni robot definition
    from a URDF file.  The model describes its kinematic and graphical
    characteristics.

    .. runblock:: pycon

        >>> import roboticstoolbox as rtb
        >>> robot = rtb.models.URDF.FrankieOmni()
        >>> print(robot)

    Defined joint configurations are:

    - qz, zero joint angle configuration, 'L' shaped configuration
    - qr, vertical 'READY' configuration
    - qs, arm is stretched out in the x-direction
    - qn, arm is at a nominal non-singular configuration

    .. codeauthor:: Jesse Haviland
    .. sectionauthor:: Peter Corke
    """

    def __init__(self):

        links_base, _, urdf_string, urdf_filepath = self.URDF_read(
            "ridgeback_description/urdf/ridgeback.urdf.xacro"
        )

        links_panda, _, urdf_string_panda, _ = self.URDF_read(
            "franka_description/robots/panda_arm_hand.urdf.xacro"
        )

        base_link = links_base[9]
        base_arm = ELink(ETS.tz(0.28), name="base_arm", parent=base_link)

        links_panda[0]._parent = base_arm
        links_panda[0]._update_fknm()
        links_all = links_base + links_panda
        links_all.append(base_arm)

        super().__init__(
            links_all,
            name="FrankieOmni",
            manufacturer="Custom",
            gripper_links=links_all[28],
            urdf_string=urdf_string,
            urdf_filepath=urdf_filepath,
        )

        self.grippers[0].tool = SE3(0, 0, 0.1034)

        self.qdlim = np.array(
            [
                4.0,
                4.0,
                4.0,
                2.1750,
                2.1750,
                2.1750,
                2.1750,
                2.6100,
                2.6100,
                2.6100,
                3.0,
                3.0,
            ]
        )

        self.addconfiguration("qz", np.array([0, 0, 0, 0, 0, 0, 0, 0, 0, 0]))

        self.addconfiguration(
            "qr", np.array([0, 0, 0, 0, -0.3, 0, -2.2, 0, 2.0, np.pi / 4])
        )


if __name__ == "__main__":  # pragma nocover
    pass

    # r = Panda()

    # for link in r.grippers[0].links:
    #     print(link)
