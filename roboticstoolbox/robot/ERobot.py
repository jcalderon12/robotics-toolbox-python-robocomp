#!/usr/bin/env python3
"""
Created on Tue Apr 24 15:48:52 2020
@author: Jesse Haviland
"""

from os.path import splitext
import tempfile
import subprocess
import webbrowser
import numpy as np
from spatialmath import SE3, SE2
from spatialmath.base.argcheck import getvector, verifymatrix, getmatrix, \
    islistof

from roboticstoolbox.robot.ELink import ELink, ELink2, BaseELink
from roboticstoolbox.robot.ETS import ETS, ETS2
from roboticstoolbox.robot.DHRobot import DHRobot
from roboticstoolbox.tools import xacro
from roboticstoolbox.tools import URDF
from roboticstoolbox.robot.Robot import Robot
from roboticstoolbox.robot.Gripper import Gripper
from roboticstoolbox.tools.data import path_to_datafile

from pathlib import PurePosixPath
from ansitable import ANSITable, Column
from spatialmath import SpatialAcceleration, SpatialVelocity, \
    SpatialInertia, SpatialForce

import fknm


class BaseERobot(Robot):

    """
    Construct an ERobot object
    :param et_list: List of elementary transforms which represent the robot
        kinematics
    :type et_list: ET list
    :param name: Name of the robot
    :type name: str, optional
    :param manufacturer: Manufacturer of the robot
    :type manufacturer: str, optional
    :param base: Location of the base is the world frame
    :type base: SE3, optional
    :param tool: Offset of the flange of the robot to the end-effector
    :type tool: SE3, optional
    :param gravity: The gravity vector
    :type n: ndarray(3)
    An ERobot represents the kinematics of a serial-link manipulator with
    one or more branches.
    From ETS
    --------
    Example:
    .. runblock:: pycon
        >>> from roboticstoolbox import ETS, ERobot
        >>> ets = ETS.rz() * ETS.ry() * ETS.tz(1) * ETS.ry() * ETS.tz(1)
        >>> robot = ERobot(ets)
        >>> print(robot)
    The ETS is partitioned such that a new link frame is created **after** every
    joint variable.
    From list of ELinks
    -------------------
    Example:
    .. runblock:: pycon
        >>> from roboticstoolbox import ETS, ERobot
        >>> link1 = ELink(ETS.rz(), name='link1')
        >>> link2 = ELink(ETS.ry(), name='link2', parent=link1)
        >>> link3 = ELink(ETS.tz(1) * ETS.ry(), name='link3', parent=link2)
        >>> link4 = ELink(ETS.tz(1), name='ee', parent=link3)
        >>> robot = ERobot([link1, link2, link3, link4])
        >>> print(robot)
    A number of ``ELink`` objects are created, each has a transform with
    respect to the previous frame, and all except the first link have a parent.
    The implicit parent of the first link is the base.
    The parent also can be specified as a string, and its name is mapped to the
    parent link by name in ``ERobot``.
    If no ``parent`` arguments are given it is assumed the links are in
    sequential order, and the parent hierarchy will be automatically
    established.
    .. runblock:: pycon
        >>> from roboticstoolbox import ETS, ERobot
        >>> robot = ERobot([
        >>>     ELink(ETS.rz(), name='link1'),
        >>>     ELink(ETS.ry(), name='link2'),
        >>>     ELink(ETS.tz(1) * ETS.ry(), name='link3'),
        >>>     ELink(ETS.tz(1), name='ee')
        >>>             ])
        >>> print(robot)
    Branched robots
    ---------------
    Example:
    .. runblock:: pycon
        >>> robot = ERobot([
        >>>    ELink(ETS.rz(), name='link1'),
        >>>    ELink(ETS.tx(1) * ETS.ty(-0.5) * ETS.rz(), name='link2', parent='link1'),
        >>>    ELink(ETS.tx(1), name='ee_1', parent='link2'),
        >>>    ELink(ETS.tx(1) * ETS.ty(0.5) * ETS.rz(), name='link3', parent='link1'),
        >>>    ELink(ETS.tx(1), name='ee_2', parent='link3')
        >>>             ])
        >>> print(robot)
    :references:
        - Kinematic Derivatives using the Elementary Transform Sequence,
          J. Haviland and P. Corke
    """  # noqa E501

    def __init__(
            self,
            links,
            base_link=None,
            gripper_links=None,
            checkjindex=True,
            **kwargs
    ):

        self._ets = []
        self._linkdict = {}
        self._n = 0
        self._ee_links = []
        self._base_link = None

        # Ordered links, we reorder the input elinks to be in depth first
        # search order
        orlinks = []

        # check all the incoming ELink objects
        n = 0
        link_number = 0
        for link in links:
            # if link has no name, give it one
            if link.name is None:
                link.name = f"link-{link_number}"
                link_number += 1

            # put it in the link dictionary, check for duplicates
            if link.name in self._linkdict:
                raise ValueError(
                    f'link name {link.name} is not unique')
            self._linkdict[link.name] = link

            if link.isjoint:
                n += 1

        # resolve parents given by name, within the context of
        # this set of links
        for link in links:
            if isinstance(link.parent, str):
                link._parent = self._linkdict[link.parent]

        if all([link.parent is None for link in links]):
            # no parent links were given, assume they are sequential
            for i in range(len(links) - 1):
                links[i + 1]._parent = links[i]

        self._n = n

        # scan for base
        for link in links:
            # is this a base link?
            if link._parent is None:
                if self._base_link is not None:
                    raise ValueError('Multiple base links')
                self._base_link = link
            else:
                # no, update children of this link's parent
                link._parent._children.append(link)

        # Set up the gripper, make a list containing the root of all
        # grippers
        if gripper_links is not None:
            if isinstance(gripper_links, ELink):
                gripper_links = [gripper_links]
        else:
            gripper_links = []

        # An empty list to hold all grippers
        self.grippers = []

        # Make a gripper object for each gripper
        for link in gripper_links:
            g_links = self.dfs_links(link)

            # Remove gripper links from the robot
            for g_link in g_links:
                links.remove(g_link)

            # Save the gripper object
            self.grippers.append(Gripper(g_links))

        # Subtract the n of the grippers from the n of the robot
        for gripper in self.grippers:
            self._n -= gripper.n

        # Set the ee links
        self.ee_links = []
        if len(gripper_links) == 0:
            for link in links:
                # is this a leaf node? and do we not have any grippers
                if len(link.children) == 0:
                    # no children, must be an end-effector
                    self.ee_links.append(link)
        else:
            for link in gripper_links:
                # use the passed in value
                self.ee_links.append(link.parent)

        # assign the joint indices
        if all([link.jindex is None for link in links]):

            jindex = [0]  # "mutable integer" hack

            def visit_link(link, jindex):
                # if it's a joint, assign it a jindex and increment it
                if link.isjoint and link in links:
                    link.jindex = jindex[0]
                    jindex[0] += 1

                if link in links:
                    orlinks.append(link)

            # visit all links in DFS order
            self.dfs_links(
                self.base_link, lambda link: visit_link(link, jindex))

        elif all([link.jindex is not None for link in links if link.isjoint]):
            # jindex set on all, check they are unique and sequential
            if checkjindex:
                jset = set(range(self._n))
                for link in links:
                    if link.isjoint and link.jindex not in jset:
                        raise ValueError(
                            f'joint index {link.jindex} was '
                            'repeated or out of range')
                    jset -= set([link.jindex])
                if len(jset) > 0:  # pragma nocover  # is impossible
                    raise ValueError(f'joints {jset} were not assigned')
            orlinks = links
        else:
            # must be a mixture of ELinks with/without jindex
            raise ValueError(
                'all links must have a jindex, or none have a jindex')

        self._nbranches = sum([link.nchildren == 0 for link in links])

        # Current joint angles of the robot
        # TODO should go to Robot class?
        self.q = np.zeros(self.n)
        self.qd = np.zeros(self.n)
        self.qdd = np.zeros(self.n)
        self.control_type = 'v'

        # Set up qlim
        qlim = np.zeros((2, self.n))
        j = 0

        for i in range(len(orlinks)):
            if orlinks[i].isjoint:
                qlim[:, j] = orlinks[i].qlim
                j += 1
        self._qlim = qlim

        for i in range(self.n):
            if np.any(qlim[:, i] != 0) and \
                    not np.any(np.isnan(qlim[:, i])):
                self._valid_qlim = True

        super().__init__(orlinks, **kwargs)

    def __str__(self):
        """
        Pretty prints the ETS Model of the robot.
        :return: Pretty print of the robot model
        :rtype: str
        .. note::
            - Constant links are shown in blue.
            - End-effector links are prefixed with an @
            - Angles in degrees
            - The robot base frame is denoted as ``BASE`` and is equal to the
              robot's ``base`` attribute.
        """
        table = ANSITable(
            Column("id", headalign="^", colalign=">"),
            Column("link", headalign="^", colalign="<"),
            Column("joint", headalign="^", colalign=">"),
            Column("parent", headalign="^", colalign="<"),
            Column("ETS", headalign="^", colalign="<"),
            border="thin")
        for k, link in enumerate(self):
            color = "" if link.isjoint else "<<blue>>"
            ee = "@" if link in self.ee_links else ""
            ets = link.ets()
            if link.parent is None:
                parent_name = "BASE"
            else:
                parent_name = link.parent.name
            s = ets.__str__(f"q{link._jindex}")
            if len(s) > 0:
                s = " \u2295 " + s

            if link.isjoint:
                if link._joint_name is not None:
                    jname = link._joint_name
                else:
                    jname = link.jindex
            else:
                jname = ''
            table.row(
                k,
                color + ee + link.name,
                jname,
                parent_name,
                f"{{{link.name}}} = {{{parent_name}}}{s}"
            )
        if isinstance(self, ERobot):
            classname = 'ERobot'
        elif isinstance(self, ERobot2):
            classname = 'ERobot2'
        s = f"{classname}: {self.name}"
        if self.manufacturer is not None and len(self.manufacturer) > 0:
            s += f" (by {self.manufacturer})"
        s += f", {self.n} joints ({self.structure})"
        if self.nbranches > 1:
            s += f", {self.nbranches} branches"
        if self._hasdynamics:
            s += ", dynamics"
        if any([len(link.geometry) > 0 for link in self.links]):
            s += ", geometry"
        if any([len(link.collision) > 0 for link in self.links]):
            s += ", collision"
        s += "\n"

        s += str(table)
        s += self.configurations_str()

        return s

    def hierarchy(self):
        """
        Pretty print the robot link hierachy
        :return: Pretty print of the robot model
        :rtype: str
        Example:
        .. runblock:: pycon
            >>> import roboticstoolbox as rtb
            >>> robot = rtb.models.URDF.Panda()
            >>> robot.hierarchy()
        """

        # link = self.base_link

        def recurse(link, indent=0):
            print(' ' * indent * 2, link.name)
            for child in link.child:
                recurse(child, indent + 1)

        recurse(self.base_link)

    @property
    def qlim(self):
        return self._qlim

    @property
    def valid_qlim(self):

        return self._valid_qlim

# --------------------------------------------------------------------- #

    @property
    def n(self):
        """
        Number of joints
        :return: number of variable joint in the robot's kinematic tree
        :rtype: int
        The sum of the number of revolute and prismatic joints.
        """
        return self._n

# --------------------------------------------------------------------- #

    @property
    def nbranches(self):
        """
        Number of branches
        :return: number of branches in the robot's kinematic tree
        :rtype: int
        Computed as the number of links with zero children
        """
        return self._nbranches
# --------------------------------------------------------------------- #

    @property
    def elinks(self):
        # return self._linkdict
        return self._links
# --------------------------------------------------------------------- #

    @property
    def link_dict(self):
        return self._linkdict
# --------------------------------------------------------------------- #

    @property
    def base_link(self):
        return self._base_link

    @base_link.setter
    def base_link(self, link):
        if isinstance(link, ELink):
            self._base_link = link
        else:
            # self._base_link = self.links[link]
            raise TypeError('Must be an ELink')
        # self._reset_fk_path()
# --------------------------------------------------------------------- #
    # TODO  get configuration string

    @property
    def ee_links(self):
        return self._ee_links

    # def add_ee(self, link):
    #     if isinstance(link, ELink):
    #         self._ee_link.append(link)
    #     else:
    #         raise ValueError('must be an ELink')
    #     self._reset_fk_path()

    @ee_links.setter
    def ee_links(self, link):
        if isinstance(link, ELink):
            self._ee_links = [link]
        elif isinstance(link, list) and \
                all([isinstance(x, ELink) for x in link]):
            self._ee_links = link
        else:
            raise TypeError('expecting an ELink or list of ELinks')
        # self._reset_fk_path()

    @property
    def reach(self):
        r"""
        Reach of the robot
        :return: Maximum reach of the robot
        :rtype: float
        A conservative estimate of the reach of the robot. It is computed as
        the sum of the translational ETs that define the link transform.
        .. note::
            - Probably an overestimate of reach
            - Used by numerical inverse kinematics to scale translational
              error.
            - For a prismatic joint, uses ``qlim`` if it is set
        .. warning:: Computed on the first access. If kinematic parameters
              subsequently change this will not be reflected.
        """
        if self._reach is None:
            d = 0
            for link in self:
                for et in link.ets():
                    if et.isprismatic:
                        d += abs(et.eta)
                if link.isprismatic and link.qlim is not None:
                    d += link.qlim[1]
            self._reach = d
        return self._reach

# --------------------------------------------------------------------- #

    # @property
    # def ets(self):
    #     return self._ets

# --------------------------------------------------------------------- #

    # @property
    # def M(self):
    #     return self._M

# --------------------------------------------------------------------- #

    # @property
    # def q_idx(self):
    #     return self._q_idx

# --------------------------------------------------------------------- #

    def ets(self, start=None, end=None, explored=None, path=None):
        """
        ERobot to ETS
        :param start: start of path, defaults to ``base_link``
        :type start: ELink or str, optional
        :param end: end of path, defaults to end-effector
        :type end: ELink or str, optional
        :raises ValueError: a link does not belong to this ERobot
        :raises TypeError: a bad link argument
        :return: elementary transform sequence
        :rtype: ETS instance
        - ``robot.ets()`` is an ETS representing the kinematics from base to
          end-effector.
        - ``robot.ets(end=link)`` is an ETS representing the kinematics from
          base to the link ``link`` specified as an ELink reference or a name.
        - ``robot.ets(start=l1, end=l2)`` is an ETS representing the kinematics
          from link ``l1`` to link ``l2``.
        .. runblock:: pycon
            >>> import roboticstoolbox as rtb
            >>> panda = rtb.models.ETS.Panda()
            >>> panda.ets()
        """
        link = self._getlink(start, self.base_link)
        if end is None and len(self.ee_links) > 1:
            raise ValueError(
                'ambiguous, specify which end-effector is required')
        end = self._getlink(end, self.ee_links[0])

        if explored is None:
            explored = set()
        toplevel = path is None

        explored.add(link)
        if link == end:
            return path

        # unlike regular DFS, the neighbours of the node are its children
        # and its parent.

        # visit child nodes below start
        if toplevel:
            path = link.ets()
        for child in link.children:
            if child not in explored:
                p = self.ets(child, end, explored, path * child.ets())
                if p is not None:
                    return p

        # we didn't find the node below, keep going up a level, and recursing
        # down again
        if toplevel:
            path = None
        if link.parent is not None:
            parent = link.parent  # go up one level toward the root
            if parent not in explored:
                if path is None:
                    p = self.ets(parent, end, explored, link.ets().inv())
                else:
                    p = self.ets(parent, end, explored, path * link.ets().inv())
                if p is not None:
                    return p
        return None


# --------------------------------------------------------------------- #

    def showgraph(self, **kwargs):
        """
        Display a link transform graph in browser
        :param etsbox: Put the link ETS in a box, otherwise an edge label
        :type etsbox: bool
        :param jtype: Arrowhead to node indicates revolute or prismatic type
        :type jtype: bool
        :param static: Show static joints in blue and bold
        :type static: bool
        ``robot.showgraph()`` displays a graph of the robot's link frames
        and the ETS between them.  It uses GraphViz dot.
        The nodes are:
            - Base is shown as a grey square.  This is the world frame origin,
              but can be changed using the ``base`` attribute of the robot.
            - Link frames are indicated by circles
            - ETS transforms are indicated by rounded boxes
        The edges are:
            - an arrow if `jtype` is False or the joint is fixed
            - an arrow with a round head if `jtype` is True and the joint is
              revolute
            - an arrow with a box head if `jtype` is True and the joint is
              prismatic
        Edge labels or nodes in blue have a fixed transformation to the
        preceding link.
        Example::
            >>> import roboticstoolbox as rtb
            >>> panda = rtb.models.URDF.Panda()
            >>> panda.showgraph()
        .. image:: ../figs/panda-graph.svg
            :width: 600
        :seealso: :func:`dotfile`
        """

        # create the temporary dotfile
        dotfile = tempfile.TemporaryFile(mode="w")
        self.dotfile(dotfile, **kwargs)

        # rewind the dot file, create PDF file in the filesystem, run dot
        dotfile.seek(0)
        pdffile = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        subprocess.run("dot -Tpdf", shell=True, stdin=dotfile, stdout=pdffile)

        # open the PDF file in browser (hopefully portable), then cleanup
        webbrowser.open(f"file://{pdffile.name}")
        # time.sleep(1)
        # os.remove(pdffile.name)

    def dotfile(self, filename, etsbox=False, jtype=False, static=True):
        """
        Write a link transform graph as a GraphViz dot file
        :param file: Name of file to write to
        :type file: str or file
        :param etsbox: Put the link ETS in a box, otherwise an edge label
        :type etsbox: bool
        :param jtype: Arrowhead to node indicates revolute or prismatic type
        :type jtype: bool
        :param static: Show static joints in blue and bold
        :type static: bool
        The file can be processed using dot::
            % dot -Tpng -o out.png dotfile.dot
        The nodes are:
            - Base is shown as a grey square.  This is the world frame origin,
              but can be changed using the ``base`` attribute of the robot.
            - Link frames are indicated by circles
            - ETS transforms are indicated by rounded boxes
        The edges are:
            - an arrow if `jtype` is False or the joint is fixed
            - an arrow with a round head if `jtype` is True and the joint is
              revolute
            - an arrow with a box head if `jtype` is True and the joint is
              prismatic
        Edge labels or nodes in blue have a fixed transformation to the
        preceding link.
        .. note:: If ``filename`` is a file object then the file will *not*
            be closed after the GraphViz model is written.
        :seealso: :func:`showgraph`
        """
        if isinstance(filename, str):
            file = open(filename, 'w')
        else:
            file = filename

        header = r"""digraph G {
graph [rankdir=LR];
"""

        def draw_edge(link, etsbox, jtype, static):
            # draw the edge
            if jtype:
                if link.isprismatic:
                    edge_options = \
                        'arrowhead="box", arrowtail="inv", dir="both"'
                elif link.isrevolute:
                    edge_options = \
                        'arrowhead="dot", arrowtail="inv", dir="both"'
                else:
                    edge_options = 'arrowhead="normal"'
            else:
                edge_options = 'arrowhead="normal"'

            if link.parent is None:
                parent = 'BASE'
            else:
                parent = link.parent.name

            if etsbox:
                # put the ets fragment in a box
                if not link.isjoint and static:
                    node_options = ', fontcolor="blue"'
                else:
                    node_options = ''
                file.write(
                    '  {}_ets [shape=box, style=rounded, '
                    'label="{}"{}];\n'.format(
                        link.name, link.ets().__str__(
                            q=f"q{link.jindex}"), node_options))
                file.write('  {} -> {}_ets;\n'.format(parent, link.name))
                file.write('  {}_ets -> {} [{}];\n'.format(
                    link.name, link.name, edge_options))
            else:
                # put the ets fragment as an edge label
                if not link.isjoint and static:
                    edge_options += 'fontcolor="blue"'
                file.write('  {} -> {} [label="{}", {}];\n'.format(
                    parent, link.name, link.ets().__str__(
                        q=f"q{link.jindex}"), edge_options))

        file.write(header)

        # add the base link
        file.write('  BASE [shape=square, style=filled, fillcolor=gray]\n')

        # add the links
        for link in self:
            # draw the link frame node (circle) or ee node (doublecircle)
            if link in self.ee_links:
                # end-effector
                node_options = \
                    'shape="doublecircle", color="blue", fontcolor="blue"'
            else:
                node_options = 'shape="circle"'

            file.write('  {} [{}];\n'.format(link.name, node_options))

            draw_edge(link, etsbox, jtype, static)

        for gripper in self.grippers:
            for link in gripper.links:
                file.write('  {} [shape=cds];\n'.format(link.name))
                draw_edge(link, etsbox, jtype, static)

        file.write('}\n')

        if isinstance(filename, str):
            file.close()  # noqa

    def dfs_links(self, start, func=None):
        """
        Visit all links from start in depth-first order and will apply
        func to each visited link
        :param start: the link to start at
        :type start: ELink
        :param func: An optional function to apply to each link as it is found
        :type func: function
        :returns: A list of links
        :rtype: list of ELink
        """
        visited = []

        def vis_children(link):
            visited.append(link)
            if func is not None:
                func(link)

            for li in link.children:
                if li not in visited:
                    vis_children(li)

        vis_children(start)

        return visited

    def _get_limit_links(self, end=None, start=None):
        """
        Get and validate an end-effector, and a base link
        :param end: end-effector or gripper to compute forward kinematics to
        :type end: str or ELink or Gripper, optional
        :param start: name or reference to a base link, defaults to None
        :type start: str or ELink, optional
        :raises ValueError: link not known or ambiguous
        :raises ValueError: [description]
        :raises TypeError: unknown type provided
        :return: end-effector link, base link, and tool transform of gripper
            if applicable
        :rtype: ELink, Elink, SE3 or None
        Helper method to find or validate an end-effector and base link.
        """

        # Try cache
        # if self._cache_end is not None:
        #     return self._cache_end, self._cache_start, self._cache_end_tool

        tool = None
        if end is None:

            # if we have a gripper, use it
            if len(self.grippers) == 1:
                end = self.grippers[0].links[0]
                tool = self.grippers[0].tool
            elif len(self.grippers) > 1:
                # if more than one gripper, user must choose
                raise ValueError('Must specify which gripper')

            # no grippers, use ee link if just one
            elif len(self.ee_links) == 1:
                end = self.ee_links[0]
            else:
                # if more than one EE, user must choose
                raise ValueError('Must specify which end-effector')

            # Cache result
            self._cache_end = end
            self._cache_end_tool = tool
        else:

            # Check if end corresponds to gripper
            for gripper in self.grippers:
                if end == gripper or end == gripper.name:
                    tool = gripper.tool
                    end = gripper.links[0]

            # otherwise check for end in the links
            end = self._getlink(end)

        if start is None:
            start = self.base_link
            # Cache result
            self._cache_start = start
        else:
            # start effector is specified
            start = self._getlink(start)

        return end, start, tool

    def _getlink(self, link, default=None):
        """
        Validate reference to ELink
        :param link: link
        :type link: ELink or str
        :raises ValueError: link does not belong to this ERobot
        :raises TypeError: bad argument
        :return: link reference
        :rtype: ELink
        ``robot._getlink(link)`` is a validated reference to an ELink within
        the ERobot ``robot``.  If ``link`` is:
        -  an ``ELink`` reference it is validated as belonging to
          ``robot``.
        - a string, then it looked up in the robot's link name dictionary, and
          an ELink reference returned.
        """
        if link is None:
            link = default

        if isinstance(link, str):
            if link in self.link_dict:
                return self.link_dict[link]

            raise ValueError(f'no link named {link}')

        elif isinstance(link, BaseELink):
            if link in self.links:
                return link
            else:
                for gripper in self.grippers:
                    if link in gripper.links:
                        return link

                raise ValueError('link not in robot links')
        else:
            raise TypeError('unknown argument')

    def fkine_path(self, q, old=None):
        '''
        Tall = robot.fkine_all(q) evaluates fkine for each joint within a
        robot and returns a trajecotry of poses.
        Tall = fkine_all() as above except uses the stored q value of the
        robot object.
        :param q: The joint angles/configuration of the robot (Optional,
            if not supplied will use the stored q values).
        :type q: float ndarray(n)
        :param old: for compatibility with DHRobot version, ignored.
        :return T: Homogeneous transformation trajectory
        :rtype T: SE3 list
        .. note::
            - The robot's base transform, if present, are incorporated
              into the result.
        :references:
            - Kinematic Derivatives using the Elementary Transform
              Sequence, J. Haviland and P. Corke
        '''
        q = getvector(q)
        T = self.base
        linkframes = []
        j = 0
        for link in self.elinks:
            if link.isjoint:
                T *= link.A(q[j])
                j += 1
            else:
                T *= link.A()
            linkframes.append(T)

        return linkframes
# =========================================================================== #


class ERobot(BaseERobot):

    def __init__(self, arg, **kwargs):

        if isinstance(arg, DHRobot):
            # we're passed a DHRobot object
            # TODO handle dynamic parameters if given
            arg = arg.ets()

        if isinstance(arg, ETS):
            # we're passed an ETS string
            ets = arg
            links = []
            # chop it up into segments, a link frame after every joint
            parent = None
            for j, ets_j in enumerate(arg.split()):
                elink = ELink(ets_j, parent=parent, name=f"link{j:d}")
                parent = elink
                links.append(elink)
            
        elif islistof(arg, ELink):
            links = arg
        else:
            raise TypeError(
                'constructor argument must be ETS or list of ELink')

        super().__init__(links, **kwargs)

        # Cached paths through links
        # TODO Add listners on setters to reset cache
        self._reset_cache()

    @classmethod
    def URDF(cls, file_path, gripper=None):
        """
        Construct an ERobot object from URDF file
        :param file_path: [description]
        :type file_path: [type]
        :param gripper: index or name of the gripper link(s)
        :type gripper: int or str or list
        :return: [description]
        :rtype: [type]
        If ``gripper`` is specified, links from that link outward are removed
        from the rigid-body tree and folded into a ``Gripper`` object.
        """
        links, name = ERobot.URDF_read(file_path)

        if gripper is not None:
            if isinstance(gripper, int):
                gripper = links[gripper]
            elif isinstance(gripper, str):
                for link in links:
                    if link.name == gripper:
                        gripper = link
                        break
                else:
                    raise ValueError(f"no link named {gripper}")
            else:
                raise TypeError('bad argument passed as gripper')

        links, name = ERobot.URDF_read(file_path)

        return cls(links, name=name, gripper_links=gripper)

    def _reset_cache(self):
        self._path_cache = {}
        self._path_cache_fknm = {}
        self._cache_end = None
        self._cache_start = None
        self._cache_end_tool = None
        self._eye_fknm = np.eye(4)

        self._cache_links_fknm = []

        self._cache_grippers = []

        for link in self.elinks:
            self._cache_links_fknm.append(link._fknm)

        for gripper in self.grippers:
            cache = []
            for link in gripper.links:
                cache.append(link._fknm)
            self._cache_grippers.append(cache)

        self._cache_m = len(self._cache_links_fknm)

    # def dfs_path(self, l1, l2):
    #     path = []
    #     visited = [l1]

    #     def vis_children(link):
    #         visited.append(link)

    #         for li in link.child:
    #             if li not in visited:

    #                 if li == l2 or vis_children(li):
    #                     path.append(li)
    #                     return True
    #     vis_children(l1)
    #     path.append(l1)
    #     path.reverse()
    #     return path

    def to_dict(self, show_robot=True, show_collision=False):

        self.fkine_links(self.q)

        ob = []

        for link in self.links:

            if show_robot:
                for gi in link.geometry:
                    ob.append(gi.to_dict())
            if show_collision:
                for gi in link.collision:
                    ob.append(gi.to_dict())

        # Do the grippers now
        for gripper in self.grippers:
            for link in gripper.links:

                if show_robot:
                    for gi in link.geometry:
                        ob.append(gi.to_dict())
                if show_collision:
                    for gi in link.collision:
                        ob.append(gi.to_dict())

        return ob

    def fk_dict(self, show_robot=True, show_collision=False):
        ob = []

        # Do the robot
        for link in self.links:

            if show_robot:
                for gi in link.geometry:
                    ob.append(gi.fk_dict())
            if show_collision:
                for gi in link.collision:
                    ob.append(gi.fk_dict())

        # Do the grippers now
        for gripper in self.grippers:
            for link in gripper.links:
                if show_robot:
                    for gi in link.geometry:
                        ob.append(gi.fk_dict())
                if show_collision:
                    for gi in link.collision:
                        ob.append(gi.fk_dict())

        return ob

    @staticmethod
    def URDF_read(file_path, tld=None):
        """
        Read a URDF file as ELinks
        :param file_path: File path relative to the xacro folder
        :type file_path: str, in Posix file path fprmat
        :param tld: top-level directory, defaults to None
        :type tld: str, optional
        :return: Links and robot name
        :rtype: tuple(ELink list, str)
        File should be specified relative to ``RTBDATA/URDF/xacro``
        """

        # get the path to the class that defines the robot
        base_path = path_to_datafile('xacro')
        # print("*** urdf_to_ets_args: ", classpath)
        # add on relative path to get to the URDF or xacro file
        # base_path = PurePath(classpath).parent.parent / 'URDF' / 'xacro'
        file_path = base_path / PurePosixPath(file_path)
        name, ext = splitext(file_path)

        if ext == '.xacro':
            # it's a xacro file, preprocess it
            if tld is not None:
                tld = base_path / PurePosixPath(tld)
            urdf_string = xacro.main(file_path, tld)
            try:
                urdf = URDF.loadstr(urdf_string, file_path)
            except BaseException as e:
                print('error parsing URDF file', file_path)
                raise e
        else:  # pragma nocover
            urdf = URDF.loadstr(open(file_path).read(), file_path)

        return urdf.elinks, urdf.name

# --------------------------------------------------------------------- #

    def fkine(
            self, q, unit='rad', end=None, start=None, tool=None,
            include_base=True, fast=False):
        '''
        Forward kinematics
        :param q: Joint coordinates
        :type q: ndarray(n) or ndarray(m,n)
        :param end: end-effector or gripper to compute forward kinematics to
        :type end: str or ELink or Gripper
        :param start: the link to compute forward kinematics from
        :type start: str or ELink
        :param tool: tool transform, optional
        :type tool: SE3
        :return: The transformation matrix representing the pose of the
            end-effector
        :rtype: SE3 instance
        - ``T = robot.fkine(q)`` evaluates forward kinematics for the robot at
          joint configuration ``q``.
        **Trajectory operation**:
        If ``q`` has multiple rows (mxn), it is considered a trajectory and the
        result is an ``SE3`` instance with ``m`` values.
        .. note::
            - For a robot with a single end-effector there is no need to
              specify ``end``
            - For a robot with multiple end-effectors, the ``end`` must
              be specified.
            - The robot's base tool transform, if set, is incorporated
              into the result.
            - A tool transform, if provided, is incorporated into the result.
            - Works from the end-effector link to the base
        :references:
            - Kinematic Derivatives using the Elementary Transform
              Sequence, J. Haviland and P. Corke
        '''

        # Use c extension to calculate fkine
        if fast:
            path, _, etool = self.get_path(end, start, _fknm=True)
            m = len(path)

            if tool is None:
                tool = self._eye_fknm

            T = np.empty((4, 4))
            fknm.fkine(m, path, q, etool, tool, T)

            if not include_base:
                return T
            else:
                return self.base.A @ T

        # Otherwise use Python method
        # we work with NumPy arrays not SE2/3 classes for speed
        q = getmatrix(q, (None, self.n))

        end, start, etool = self._get_limit_links(end, start)

        if etool is not None and tool is not None:
            tool = (etool * tool).A
        elif etool is not None:
            tool = etool.A
        elif tool is not None:
            tool = tool.A

        if tool is None and self._tool is not None:
            tool = self._tool.A

        T = SE3.Empty()

        for k, qk in enumerate(q):
            if unit == 'deg':
                qk = self.toradians(qk)
            link = end  # start with last link

            # add tool if provided
            A = link.A(qk[link.jindex], fast=True)
            if A is None:
                Tk = tool
            else:
                if tool is None:
                    Tk = A
                elif A is not None:
                    Tk = A @ tool

            # add remaining links, back toward the base
            while True:
                link = link.parent

                if link is None:
                    break

                A = link.A(qk[link.jindex], fast=True)

                if A is not None:
                    Tk = A @ Tk

                if link is start:
                    break

            # add base transform if it is set
            if self._base is not None and start == self.base_link:
                Tk = self.base.A @ Tk

            # cast to pose class and append
            T.append(T.__class__(Tk, check=False))

        return T

    def fkine_all(self, q, old=None):
        '''
        Tall = robot.fkine_all(q) evaluates fkine for each joint within a
        robot and returns a trajecotry of poses.
        Tall = fkine_all() as above except uses the stored q value of the
        robot object.
        :param q: The joint angles/configuration of the robot (Optional,
            if not supplied will use the stored q values).
        :type q: float ndarray(n)
        :param old: for compatibility with DHRobot version, ignored.
        :return T: Homogeneous transformation trajectory
        :rtype T: SE3 list
        .. note::
            - The robot's base transform, if present, are incorporated
              into the result.
        :references:
            - Kinematic Derivatives using the Elementary Transform
              Sequence, J. Haviland and P. Corke
        '''

        fknm.fkine_all(
            self._cache_m,
            self._cache_links_fknm,
            q,
            self._base.A)

        for i in range(len(self._cache_grippers)):
            fknm.fkine_all(
                len(self._cache_grippers[i]),
                self._cache_grippers[i],
                self.grippers[i].q,
                self._base.A)

        # for link in self.elinks:

        #     # Update the link model transforms as well
        #     for col in link.collision:
        #         col.wT = link._fk

        #     for gi in link.geometry:
        #         gi.wT = link._fk

        # # Do the grippers now
        # for gripper in self.grippers:
        #     for link in gripper.links:

        #         # Update the link model transforms as well
        #         for col in link.collision:
        #             col.wT = link._fk

        #         for gi in link.geometry:
        #             gi.wT = link._fk

    def fkine_links(self, q):
        '''
        robot.fkine_links(q) evaluates fkine for each link within a
        robot and stores that pose within the link.

        :param q: The joint angles/configuration of the robot
        :type q: float ndarray(n)

        .. note::

            - The robot's base transform, if present, are incorporated
              into the result.

        :references:
            - Kinematic Derivatives using the Elementary Transform
              Sequence, J. Haviland and P. Corke

        '''

        fknm.fkine_all(
            self._cache_m,
            self._cache_links_fknm,
            q,
            self._base.A)

        for i in range(len(self._cache_grippers)):
            fknm.fkine_all(
                len(self._cache_grippers[i]),
                self._cache_grippers[i],
                self.grippers[i].q,
                self._base.A)

    def get_path(self, end=None, start=None, _fknm=False):
        """
        Find a path from start to end. The end must come after
        the start (ie end must be further away from the base link
        of the robot than start) in the kinematic chain and both links
        must be a part of the same branch within the robot structure. This
        method is a work in progress while an approach which generalises
        to all applications is designed.
        :param end: end-effector or gripper to compute forward kinematics to
        :type end: str or ELink or Gripper, optional
        :param start: name or reference to a base link, defaults to None
        :type start: str or ELink, optional
        :raises ValueError: link not known or ambiguous
        :return: the path from start to end
        :rtype: list of Link
        """
        path = []
        n = 0

        end, start, tool = self._get_limit_links(end, start)

        # This is way faster than doing if x in y method
        try:
            if _fknm:
                return self._path_cache_fknm[start.name][end.name]
            else:
                return self._path_cache[start.name][end.name]
        except KeyError:
            pass

        if start.name not in self._path_cache:
            self._path_cache[start.name] = {}
            self._path_cache_fknm[start.name] = {}

        link = end

        path.append(link)
        if link.isjoint:
            n += 1

        while link != start:
            link = link.parent
            if link is None:
                raise ValueError(
                    f'cannot find path from {start.name} to'
                    f' {end.name}')
            path.append(link)
            if link.isjoint:
                n += 1

        path.reverse()
        path_fknm = [x._fknm for x in path]

        if tool is None:
            tool = SE3()

        self._path_cache[start.name][end.name] = (path, n, tool)
        self._path_cache_fknm[start.name][end.name] = (path_fknm, n, tool.A)

        if _fknm:
            return path_fknm, n, tool.A
        else:
            return path, n, tool

    def jacob0(
            self, q, end=None, start=None, tool=None, T=None,
            half=None, analytical=None, fast=False):
        r"""
        Manipulator geometric Jacobian in the base frame
        :param q: Joint coordinate vector
        :type q: ndarray(n)
        :param end: the particular link or gripper whose velocity the Jacobian
            describes, defaults to the end-effector if only one is present
        :type end: str or ELink or Gripper
        :param start: the link considered as the base frame, defaults to the robots's base frame
        :type start: str or ELink
        :param tool: a static tool transformation matrix to apply to the
            end of end, defaults to None
        :type tool: SE3, optional
        :param T: The transformation matrix of the reference point which the
            Jacobian represents with respect to the base frame. Use this to
            avoid caluclating forward kinematics to save time, defaults
            to None
        :type T: SE3, optional
        :param half: return half Jacobian: 'trans' or 'rot'
        :type half: str
        :param analytical: return analytical Jacobian instead of geometric Jacobian (default)
        :type analytical: str
        :return J: Manipulator Jacobian in the base frame
        :rtype: ndarray(6,n)
        - ``robot.jacobo(q)`` is the manipulator Jacobian matrix which maps
          joint  velocity to end-effector spatial velocity expressed in the
          end-effector frame.
        End-effector spatial velocity :math:`\nu = (v_x, v_y, v_z, \omega_x, \omega_y, \omega_z)^T`
        is related to joint velocity by :math:`{}^{E}\!\nu = \mathbf{J}_m(q) \dot{q}`.
        ``analytical`` can be one of:
            =============  ==================================
            Value          Rotational representation
            =============  ==================================
            ``'rpy-xyz'``  RPY angular rates in XYZ order
            ``'rpy-zyx'``  RPY angular rates in XYZ order
            ``'eul'``      Euler angular rates in ZYZ order
            ``'exp'``      exponential coordinate rates
            =============  ==================================
        Example:
        .. runblock:: pycon
            >>> import roboticstoolbox as rtb
            >>> puma = rtb.models.ETS.Puma560()
            >>> puma.jacobe([0, 0, 0, 0, 0, 0])
        .. warning:: This is the geometric Jacobian as described in texts by
            Corke, Spong etal., Siciliano etal.  The end-effector velocity is
            described in terms of translational and angular velocity, not a 
            velocity twist as per the text by Lynch & Park.
        .. warning:: ``start`` and ``end`` must be on the same branch,
            with ``start`` closest to the base.
        """  # noqa

        # Use c extension
        if fast:
            path, n, etool = self.get_path(end, start, _fknm=True)
            if tool is None:
                tool = self._eye_fknm
            J = np.empty((6, n))
            fknm.jacob0(len(path), n, path, q, etool, tool, J)
            return J

        # Otherwise use Python
        if tool is None:
            tool = SE3()

        path, n, _ = self.get_path(end, start)

        q = getvector(q, self.n)

        if T is None:
            T = self.fkine(q, end=end, start=start, include_base=False) * tool

        T = T.A
        U = np.eye(4)
        j = 0
        J = np.zeros((6, n))
        zero = np.array([0, 0, 0])

        for link in path:

            if link.isjoint:
                U = U @ link.A(q[link.jindex], fast=True)

                if link == end:
                    U = U @ tool.A

                Tu = np.linalg.inv(U) @ T
                n = U[:3, 0]
                o = U[:3, 1]
                a = U[:3, 2]
                x = Tu[0, 3]
                y = Tu[1, 3]
                z = Tu[2, 3]

                if link.v.axis == 'Rz':
                    J[:3, j] = (o * x) - (n * y)
                    J[3:, j] = a

                elif link.v.axis == 'Ry':
                    J[:3, j] = (n * z) - (a * x)
                    J[3:, j] = o

                elif link.v.axis == 'Rx':
                    J[:3, j] = (a * y) - (o * z)
                    J[3:, j] = n

                elif link.v.axis == 'tx':
                    J[:3, j] = n
                    J[3:, j] = zero

                elif link.v.axis == 'ty':
                    J[:3, j] = o
                    J[3:, j] = zero

                elif link.v.axis == 'tz':
                    J[:3, j] = a
                    J[3:, j] = zero

                j += 1
            else:
                A = link.A(fast=True)
                if A is not None:
                    U = U @ A

        # compute rotational transform if analytical Jacobian required
        if analytical is not None and half != 'trans':

            if analytical == 'rpy-xyz':
                rpy = tr2rpy(T, 'xyz')
                A = rpy2jac(rpy, 'xyz')
            elif analytical == 'rpy-zyx':
                rpy = tr2rpy(T, 'zyx')
                A = rpy2jac(rpy, 'zyx')
            elif analytical == 'eul':
                eul = tr2eul(T)
                A = eul2jac(eul)
            elif analytical == 'exp':
                # TODO: move to SMTB.base, Horner form with skew(v)
                (theta, v) = trlog(t2r(T))
                A = np.eye(3, 3) - (1 - math.cos(theta)) / theta * skew(v) \
                    + (theta - math.sin(theta)) / theta * skew(v)**2
            else:
                raise ValueError('bad order specified')

            J = block_diag(np.eye(3, 3), np.linalg.inv(A)) @ J

        # TODO optimize computation above if half matrix is returned

        # return top or bottom half if asked
        if half is not None:
            if half == 'trans':
                J = J[:3, :]
            elif half == 'rot':
                J = J[3:, :]
            else:
                raise ValueError('bad half specified')

        return J

    def jacobe(self, q, end=None, start=None, tool=None, T=None):
        r"""
        Manipulator geometric Jacobian in the end-effector frame
        :param q: Joint coordinate vector
        :type q: ndarray(n)
        :param end: the particular link or Gripper whose velocity the Jacobian
            describes, defaults to the end-effector if only one is present
        :type end: str or ELink or Gripper
        :param start: the link considered as the base frame, defaults to the robots's base frame
        :type start: str or ELink
        :param tool: a static tool transformation matrix to apply to the
            end of end, defaults to None
        :type tool: SE3, optional
        :param T: The transformation matrix of the reference point which the
            Jacobian represents with respect to the base frame.
        :type T: SE3, optional
        :return J: Manipulator Jacobian in the end-effector frame
        :rtype: ndarray(6,n)
        - ``robot.jacobe(q)`` is the manipulator Jacobian matrix which maps
          joint  velocity to end-effector spatial velocity expressed in the
          end-effector frame.
        End-effector spatial velocity :math:`\nu = (v_x, v_y, v_z, \omega_x, \omega_y, \omega_z)^T`
        is related to joint velocity by :math:`{}^{E}\!\nu = \mathbf{J}_m(q) \dot{q}`.
        Example:
        .. runblock:: pycon
            >>> import roboticstoolbox as rtb
            >>> puma = rtb.models.ETS.Puma560()
            >>> puma.jacobe([0, 0, 0, 0, 0, 0])
        .. warning:: This is the **geometric Jacobian** as described in texts by
            Corke, Spong etal., Siciliano etal.  The end-effector velocity is
            described in terms of translational and angular velocity, not a 
            velocity twist as per the text by Lynch & Park.
        .. warning:: ``start`` and ``end`` must be on the same branch,
            with ``start`` closest to the base.
        .. note:: ``T`` can be passed in to save the cost of computing forward
            kinematics which is needed to transform velocity from end-effector
            frame to world frame.
        """  # noqa

        q = getvector(q, self.n)

        if tool is None:
            tool = SE3()

        end, start, _ = self._get_limit_links(end, start)

        if T is None:
            T = self.base.inv() * \
                self.fkine(q, end=end, start=start) * tool

        J0 = self.jacob0(q, end, start, tool, T)
        Je = self.jacobev(q, end, start, tool, T) @ J0
        return Je

    def partial_fkine0(self, q, n, J0=None, end=None, start=None):
        end, start, _ = self._get_limit_links(end, start)

        def cross(a, b):
            x = a[1] * b[2] - a[2] * b[1]
            y = a[2] * b[0] - a[0] * b[2]
            z = a[0] * b[1] - a[1] * b[0]
            return np.array([x, y, z])

        _, nl, _ = self.get_path(end, start)

        J = self.jacob0(q, end=end, start=start)
        H = self.hessian0(q, J, end, start)

        d = [J, H]
        size = [6, nl, nl]
        count = np.array([0, 0])
        c = 2

        def add_indices(indices, c):
            total = len(indices * 2)
            new_indices = []

            for i in range(total):
                j = i // 2
                new_indices.append([])
                new_indices[i].append(indices[j][0].copy())
                new_indices[i].append(indices[j][1].copy())

                # if even number
                if i % 2 == 0:
                    new_indices[i][0].append(c)
                # if odd number
                else:
                    new_indices[i][1].append(c)

            return new_indices

        def add_pdi(pdi):
            total = len(pdi * 2)
            new_pdi = []

            for i in range(total):
                j = i // 2
                new_pdi.append([])
                new_pdi[i].append(pdi[j][0])
                new_pdi[i].append(pdi[j][1])

                # if even number
                if i % 2 == 0:
                    new_pdi[i][0] += 1
                # if odd number
                else:
                    new_pdi[i][1] += 1

            return new_pdi

        # these are the indices used for the hessian
        indices = [[[1], [0]]]

        # the are the pd indices used in the corss prods
        pdi = [[0, 0]]

        while len(d) != n:
            size.append(nl)
            count = np.r_[count, 0]
            indices = add_indices(indices, c)
            pdi = add_pdi(pdi)
            c += 1

            pd = np.zeros(size)

            for i in range(nl ** c):

                rot = np.zeros(3)
                trn = np.zeros(3)

                for j in range(len(indices)):
                    pdr0 = d[pdi[j][0]]
                    pdr1 = d[pdi[j][1]]

                    idx0 = count[indices[j][0]]
                    idx1 = count[indices[j][1]]

                    rot += cross(
                        pdr0[(slice(3, 6), *idx0)],
                        pdr1[(slice(3, 6), *idx1)])

                    trn += cross(
                        pdr0[(slice(3, 6), *idx0)],
                        pdr1[(slice(0, 3), *idx1)])

                pd[(slice(0, 3), *count)] = trn
                pd[(slice(3, 6), *count)] = rot

                count[0] += 1
                for j in range(len(count)):
                    if count[j] == nl:
                        count[j] = 0
                        if (j != len(count) - 1):
                            count[j + 1] += 1

            d.append(pd)

        return d[-1]

    # def third(self, q=None, J0=None, end=None, start=None):
    #     end, start = self._get_limit_links(end, start)
    #     path, n = self.get_path(end, start)

    #     def cross(a, b):
    #         x = a[1] * b[2] - a[2] * b[1]
    #         y = a[2] * b[0] - a[0] * b[2]
    #         z = a[0] * b[1] - a[1] * b[0]
    #         return np.array([x, y, z])

    #     if J0 is None:
    #         q = getvector(q, n)
    #         J0 = self.jacob0(q, end=end)
    #     else:
    #         verifymatrix(J0, (6, n))

    #     H0 = self.hessian0(q, J0, end, start)

    #     L = np.zeros((6, n, n, n))

    #     for l in range(n):
    #         for k in range(n):
    #             for j in range(n):

    #                 L[:3, j, k, l] = cross(H0[3:, k, l], J0[:3, j]) + \
    #                     cross(J0[3:, k], H0[:3, j, l])

    #                 L[3:, j, k, l] = cross(H0[3:, k, l], J0[3:, j]) + \
    #                     cross(J0[3:, k], H0[3:, j, l])

    #     return L

    def hessian0(self, q=None, J0=None, end=None, start=None):
        r"""
        Manipulator Hessian
        The manipulator Hessian tensor maps joint acceleration to end-effector
        spatial acceleration, expressed in the world-coordinate frame. This
        function calulcates this based on the ETS of the robot. One of J0 or q
        is required. Supply J0 if already calculated to save computation time
        :param q: The joint angles/configuration of the robot (Optional,
            if not supplied will use the stored q values).
        :type q: float ndarray(n)
        :param J0: The manipulator Jacobian in the 0 frame
        :type J0: float ndarray(6,n)
        :param end: the final link/Gripper which the Hessian represents
        :type end: str or ELink or Gripper
        :param start: the first link which the Hessian represents
        :type start: str or ELink
        :return: The manipulator Hessian in 0 frame
        :rtype: float ndarray(6,n,n)
        This method computes the manipulator Hessian in the base frame.  If
        we take the time derivative of the differential kinematic relationship
        .. math::
            \nu    &= \mat{J}(\vec{q}) \dvec{q} \\
            \alpha &= \dmat{J} \dvec{q} + \mat{J} \ddvec{q}
        where
        .. math::
            \dmat{J} = \mat{H} \dvec{q}
        and :math:`\mat{H} \in \mathbb{R}^{6\times n \times n}` is the
        Hessian tensor.
        The elements of the Hessian are
        .. math::
            \mat{H}_{i,j,k} =  \frac{d^2 u_i}{d q_j d q_k}
        where :math:`u = \{t_x, t_y, t_z, r_x, r_y, r_z\}` are the elements
        of the spatial velocity vector.
        Similarly, we can write
        .. math::
            \mat{J}_{i,j} = \frac{d u_i}{d q_j}
        :references:
            - Kinematic Derivatives using the Elementary Transform
              Sequence, J. Haviland and P. Corke
        """

        end, start, _ = self._get_limit_links(end, start)
        path, n, _ = self.get_path(end, start)

        def cross(a, b):
            x = a[1] * b[2] - a[2] * b[1]
            y = a[2] * b[0] - a[0] * b[2]
            z = a[0] * b[1] - a[1] * b[0]
            return np.array([x, y, z])

        if J0 is None:
            q = getvector(q, n)
            J0 = self.jacob0(q, end=end, start=start)
        else:
            verifymatrix(J0, (6, n))

        H = np.zeros((6, n, n))

        for j in range(n):
            for i in range(j, n):

                H[:3, i, j] = cross(J0[3:, j], J0[:3, i])
                H[3:, i, j] = cross(J0[3:, j], J0[3:, i])

                if i != j:
                    H[:3, j, i] = H[:3, i, j]

        return H

    def jacobev(
            self, q, end=None, start=None,
            tool=None, T=None):
        """
        Jv = jacobev(q) is the spatial velocity Jacobian, at joint
        configuration q, which relates the velocity in the base frame to the
        velocity in the end-effector frame.
        :param q: Joint coordinate vector
        :type q: ndarray(n)
        :param end: the final link or Gripper which the Jacobian represents
        :type end: str or ELink or Gripper
        :param start: the first link which the Jacobian represents
        :type start: str or ELink
        :param tool: a static tool transformation matrix to apply to the
            end of end, defaults to None
        :type tool: SE3, optional
        :param T: The transformation matrix of the reference point which the
            Jacobian represents with respect to the base frame. Use this to
            avoid caluclating forward kinematics to save time, defaults
            to None
        :type T: SE3, optional
        :returns J: The velocity Jacobian in ee frame
        :rtype J: float ndarray(6,6)
        """

        end, start, _ = self._get_limit_links(end, start)

        if T is None:
            T = self.base.inv() * \
                self.fkine(q, end=end, start=start)
            if tool is not None:
                T *= tool
        R = (T.R).T

        Jv = np.zeros((6, 6))
        Jv[:3, :3] = R
        Jv[3:, 3:] = R

        return Jv

    def jacob0v(
            self, q, end=None, start=None,
            tool=None, T=None):
        """
        Jv = jacob0v(q) is the spatial velocity Jacobian, at joint
        configuration q, which relates the velocity in the end-effector frame
        to velocity in the base frame
        :param q: Joint coordinate vector
        :type q: ndarray(n)
        :param end: the final link or Gripper which the Jacobian represents
        :type end: str or ELink or Gripper
        :param start: the first link which the Jacobian represents
        :type start: str or ELink
        :param tool: a static tool transformation matrix to apply to the
            end of end, defaults to None
        :type tool: SE3, optional
        :param T: The transformation matrix of the reference point which the
            Jacobian represents with respect to the base frame. Use this to
            avoid caluclating forward kinematics to save time, defaults
            to None
        :type T: SE3, optional
        :returns J: The velocity Jacobian in 0 frame
        :rtype J: float ndarray(6,6)
        """

        end, start, _ = self._get_limit_links(end, start)

        if T is None:
            T = self.base.inv() * \
                self.fkine(q, end=end, start=start)
            if tool is not None:
                T *= tool
        R = (T.R)

        Jv = np.zeros((6, 6))
        Jv[:3, :3] = R
        Jv[3:, 3:] = R

        return Jv

    def link_collision_damper(
            self, shape, q=None, di=0.3, ds=0.05, xi=1.0,
            end=None, start=None):
        '''
        Formulates an inequality contraint which, when optimised for will
        make it impossible for the robot to run into a collision. Requires
        See examples/neo.py for use case
        :param ds: The minimum distance in which a joint is allowed to
            approach the collision object shape
        :type ds: float
        :param di: The influence distance in which the velocity
            damper becomes active
        :type di: float
        :param xi: The gain for the velocity damper
        :type xi: float
        :param from_link: The first link to consider, defaults to the base
            link
        :type from_link: ELink
        :param to_link: The last link to consider, will consider all links
            between from_link and to_link in the robot, defaults to the
            end-effector link
        :type to_link: ELink
        :returns: Ain, Bin as the inequality contraints for an omptimisor
        :rtype: ndarray(6), ndarray(6)
        '''

        if start is None:
            start = self.base_link

        if end is None:
            end = self.ee_link

        links, n, _ = self.get_path(start=start, end=end)

        if q is None:
            q = np.copy(self.q)
        else:
            q = getvector(q, n)

        j = 0
        Ain = None
        bin = None

        def indiv_calculation(link, link_col, q):
            d, wTlp, wTcp = link_col.closest_point(shape, di)

            if d is not None:
                lpTcp = wTlp.inv() * wTcp
                norm = lpTcp.t / d
                norm_h = np.expand_dims(np.r_[norm, 0, 0, 0], axis=0)

                Je = self.jacobe(
                    q, start=self.base_link, end=link,
                    tool=link_col.base)
                n_dim = Je.shape[1]
                dp = norm_h @ shape.v
                l_Ain = np.zeros((1, n))
                l_Ain[0, :n_dim] = norm_h @ Je
                l_bin = (xi * (d - ds) / (di - ds)) + dp
            else:
                l_Ain = None
                l_bin = None

            return l_Ain, l_bin, d, wTcp

        for link in links:
            if link.isjoint:
                j += 1

            for link_col in link.collision:
                l_Ain, l_bin, d, wTcp = indiv_calculation(
                    link, link_col, q)

                if l_Ain is not None and l_bin is not None:
                    if Ain is None:
                        Ain = l_Ain
                    else:
                        Ain = np.r_[Ain, l_Ain]

                    if bin is None:
                        bin = np.array(l_bin)
                    else:
                        bin = np.r_[bin, l_bin]

        return Ain, bin

    # inverse dynamics (recursive Newton-Euler) using spatial vector notation
    def rne(robot, q, qd, qdd, symbolic=False, gravity=None):

        n = robot.n

        # allocate intermediate variables
        Xup = SE3.Alloc(n)
        Xtree = SE3.Alloc(n)

        v = SpatialVelocity.Alloc(n)
        a = SpatialAcceleration.Alloc(n)
        f = SpatialForce.Alloc(n)
        I = SpatialInertia.Alloc(n)  # noqa
        s = [None for i in range(n)]   # joint motion subspace
        if symbolic:
            Q = np.empty((n,), dtype='O')   # joint torque/force
        else:
            Q = np.empty((n,))              # joint torque/force

        # initialize intermediate variables
        for j, link in enumerate(robot):
            I[j] = SpatialInertia(m=link.m, r=link.r)
            if symbolic and link.Ts is None:
                Xtree[j] = SE3(np.eye(4, dtype='O'), check=False)
            else:
                Xtree[j] = SE3(link.Ts, check=False)
            s[j] = link.v.s

        if gravity is None:
            a_grav = -SpatialAcceleration(robot.gravity)
        else:
            a_grav = -SpatialAcceleration(gravity)

        # forward recursion
        for j in range(0, n):
            vJ = SpatialVelocity(s[j] * qd[j])

            # transform from parent(j) to j
            Xup[j] = robot[j].A(q[j]).inv()

            if robot[j].parent is None:
                v[j] = vJ
                a[j] = Xup[j] * a_grav + SpatialAcceleration(s[j] * qdd[j])
            else:
                jp = robot[j].parent.jindex
                v[j] = Xup[j] * v[jp] + vJ
                a[j] = Xup[j] * a[jp] \
                    + SpatialAcceleration(s[j] * qdd[j]) \
                    + v[j] @ vJ

            f[j] = I[j] * a[j] + v[j] @ (I[j] * v[j])

        # backward recursion
        for j in reversed(range(0, n)):

            # next line could be np.dot(), but fails for symbolic arguments
            Q[j] = np.sum(f[j].A * s[j])

            if robot[j].parent is not None:
                jp = robot[j].parent.jindex
                f[jp] = f[jp] + Xup[j] * f[j]

        return Q


# =========================================================================== #

class ERobot2(BaseERobot):

    def __init__(self, arg, **kwargs):

        if isinstance(arg, ETS2):
            # we're passed an ETS string
            ets = arg
            links = []
            # chop it up into segments, a link frame after every joint
            parent = None
            for j, ets_j in enumerate(arg.split()):
                elink = ELink2(ets_j, parent=parent, name=f"link{j:d}")
                parent = elink
                links.append(elink)

        elif islistof(arg, ELink2):
            links = arg
        else:
            raise TypeError(
                'constructor argument must be ETS2 or list of ELink2')

        super().__init__(links, **kwargs)

        # should just set it to None
        self.base = SE2()  # override superclass

    def jacob0(self, q):
        return self.ets().jacob0(q)

    def jacobe(self, q):
        return self.ets().jacobe(q)

    def fkine(self, q, unit='rad', end=None, start=None):

        return self.ets(start, end).eval(q, unit=unit)
# --------------------------------------------------------------------- #

    def plot(
            self, q, block=True, dt=0.05, limits=None,
            vellipse=False, fellipse=False,
            eeframe=True, name=False):
        """
        2D Graphical display and animation
        :param block: Block operation of the code and keep the figure open
        :type block: bool
        :param q: The joint configuration of the robot (Optional,
            if not supplied will use the stored q values).
        :type q: float ndarray(n)
        :param dt: if q is a trajectory, this describes the delay in
            milliseconds between frames
        :type dt: int
        :param limits: Custom view limits for the plot. If not supplied will
            autoscale, [x1, x2, y1, y2, z1, z2]
        :type limits: ndarray(6)
        :param vellipse: (Plot Option) Plot the velocity ellipse at the
            end-effector
        :type vellipse: bool
        :param vellipse: (Plot Option) Plot the force ellipse at the
            end-effector
        :type vellipse: bool
        :param eeframe: (Plot Option) Plot the end-effector coordinate frame
            at the location of the end-effector. Uses three arrows, red,
            green and blue to indicate the x, y, and z-axes.
        :type eeframe: bool
        :param name: (Plot Option) Plot the name of the robot near its base
        :type name: bool
        :return: A reference to the PyPlot object which controls the
            matplotlib figure
        :rtype: PyPlot
        - ``robot.plot2(q)`` displays a 2D graphical view of a robot based on
          the kinematic model and the joint configuration ``q``. This is a
          stick figure polyline which joins the origins of the link coordinate
          frames. The plot will autoscale with an aspect ratio of 1.
        If ``q`` (m,n) representing a joint-space trajectory it will create an
        animation with a pause of ``dt`` seconds between each frame.
        .. note::
            - By default this method will block until the figure is dismissed.
              To avoid this set ``block=False``.
            - The polyline joins the origins of the link frames, but for
              some Denavit-Hartenberg models those frames may not actually
              be on the robot, ie. the lines to not neccessarily represent
              the links of the robot.
        :seealso: :func:`teach2`
        """

        # Make an empty 2D figure
        env = self._get_graphical_backend('pyplot2')

        q = getmatrix(q, (None, self.n))
        # q = getmatrix(q, (None, self.n))

        # Add the self to the figure in readonly mode
        if q.shape[0] == 1:
            env.launch(self.name + ' Plot', limits)
        else:
            env.launch(self.name + ' Trajectory Plot', limits)

        env.add(
            self, readonly=True,
            eeframe=eeframe, name=name)

        if vellipse:
            vell = self.vellipse(centre='ee')
            env.add(vell)

        if fellipse:
            fell = self.fellipse(centre='ee')
            env.add(fell)

        for qk in q:
            self.q = qk
            env.step()

        # Keep the plot open
        if block:           # pragma: no cover
            env.hold()

        return env

    def teach(
            self,
            q=None,
            block=True,
            limits=None,
            vellipse=False,
            fellipse=False,
            eeframe=True,
            name=False,
            unit='rad',
            backend='pyplot2'):
        """
        2D Graphical teach pendant
        :param block: Block operation of the code and keep the figure open
        :type block: bool
        :param q: The joint configuration of the robot (Optional,
            if not supplied will use the stored q values).
        :type q: float ndarray(n)
        :param limits: Custom view limits for the plot. If not supplied will
            autoscale, [x1, x2, y1, y2]
        :type limits: array_like(4)
        :param vellipse: (Plot Option) Plot the velocity ellipse at the
            end-effector
        :type vellipse: bool
        :param vellipse: (Plot Option) Plot the force ellipse at the
            end-effector
        :type vellipse: bool
        :param eeframe: (Plot Option) Plot the end-effector coordinate frame
            at the location of the end-effector. Uses three arrows, red,
            green and blue to indicate the x, y, and z-axes.
        :type eeframe: bool
        :param name: (Plot Option) Plot the name of the robot near its base
        :type name: bool
        :param unit: angular units: 'rad' [default], or 'deg'
        :type unit: str

        :return: A reference to the PyPlot object which controls the
            matplotlib figure
        :rtype: PyPlot
        - ``robot.teach2(q)`` creates a 2D matplotlib plot which allows the
          user to "drive" a graphical robot using a graphical slider panel.
          The robot's inital joint configuration is ``q``. The plot will
          autoscale with an aspect ratio of 1.
        - ``robot.teach2()`` as above except the robot's stored value of ``q``
          is used.
        .. note::
            - Program execution is blocked until the teach window is
              dismissed.  If ``block=False`` the method is non-blocking but
              you need to poll the window manager to ensure that the window
              remains responsive.
            - The slider limits are derived from the joint limit properties.
              If not set then:
                - For revolute joints they are assumed to be [-pi, +pi]
                - For prismatic joint they are assumed unknown and an error
                  occurs.
              If not set then
                - For revolute joints they are assumed to be [-pi, +pi]
                - For prismatic joint they are assumed unknown and an error
                  occurs.
        """

        if q is None:
            q = np.zeros((self.n,))
        else:
            q = getvector(q, self.n)

        if unit == 'deg':
            q = self.toradians(q)

        # Make an empty 3D figure
        env = self._get_graphical_backend(backend)

        # Add the robot to the figure in readonly mode
        env.launch('Teach ' + self.name, limits=limits)
        env.add(
            self, readonly=True,
            eeframe=eeframe, name=name)

        env._add_teach_panel(self, q)

        if limits is None:
            limits = np.r_[-1, 1, -1, 1] * self.reach * 1.5
            env.ax.set_xlim([limits[0], limits[1]])
            env.ax.set_ylim([limits[2], limits[3]])

        if vellipse:
            vell = self.vellipse(centre='ee', scale=0.5)
            env.add(vell)

        if fellipse:
            fell = self.fellipse(centre='ee')
            env.add(fell)

        # Keep the plot open
        if block:           # pragma: no cover
            env.hold()

        return env



if __name__ == "__main__":  # pragma nocover

    # import roboticstoolbox as rtb
    np.set_printoptions(precision=4, suppress=True)

    robot = ERobot([
        ELink(ETS.rz(), name='link1'),
        ELink(
            ETS.tx(1) * ETS.ty(-0.5) * ETS.rz(),
            name='link2', parent='link1'),
        ELink(ETS.tx(1), name='ee_1', parent='link2'),
        ELink(
            ETS.tx(1) * ETS.ty(0.5) * ETS.rz(),
            name='link3', parent='link1'),
        ELink(ETS.tx(1), name='ee_2', parent='link3')
    ])
    print(robot)

    # p = rtb.models.URDF.Puma560()
    # p.fkine(p.qz)
    # p.jacob0(p.qz)
    # p.jacobe(p.qz)

    # robot = rtb.models.ETS.Panda()
    # print(robot)
    # print(robot.base, robot.tool)
    # print(robot.ee_links)
    # ets = robot.ets()
    # print(ets)
    # print('n', ets.n)
    # ets2 = ets.compile()
    # print(ets2)

    # q = np.random.rand(7)
    # # print(ets.eval(q))
    # # print(ets2.eval(q))

    # J1 = robot.jacob0(q)
    # J2 = ets2.jacob0(q)
    # print(J1-J2)

    # print(robot[2].v, robot[2].v.jindex)
    # print(robot[2].Ts)
