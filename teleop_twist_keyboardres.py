#!/usr/bin/env python

from __future__ import print_function
import rosnode
import tf_conversions
import threading

import roslib; roslib.load_manifest('teleop_twist_keyboard')
import rospy

from geometry_msgs.msg import Twist

import sys, select, termios, tty

import random
import math
import numpy as np
from cvxopt import matrix
from cvxopt.blas import dot
from cvxopt.solvers import qp, options
from cvxopt import matrix

import qpsolvers
from qpsolvers import solve_qp
from scipy import sparse
# Unused for now, will include later for speed.
# import quadprog as solver2

import itertools
import numpy as np
from scipy.special import comb
from geometry_msgs.msg import TransformStamped, PoseStamped

options['show_progress'] = False
# Change default options of CVXOPT for faster solving
options['reltol'] = 1e-2 # was e-2
options['feastol'] = 1e-2 # was e-4
options['maxiters'] = 50 # default is 100

def at_pose(states, poses, position_error=0.05, rotation_error=0.2):
    """Checks whether robots are "close enough" to poses

    states: 3xN numpy array (of unicycle states)
    poses: 3xN numpy array (of desired states)

    -> 1xN numpy index array (of agents that are close enough)
    """
    #Check user input types
    assert isinstance(states, np.ndarray), "In the at_pose function, the robot current state argument (states) must be a numpy ndarray. Recieved type %r." % type(states).__name__
    assert isinstance(poses, np.ndarray), "In the at_pose function, the checked pose argument (poses) must be a numpy ndarray. Recieved type %r." % type(poses).__name__
    assert isinstance(position_error, (float,int)), "In the at_pose function, the allowable position error argument (position_error) must be an integer or float. Recieved type %r." % type(position_error).__name__
    assert isinstance(rotation_error, (float,int)), "In the at_pose function, the allowable angular error argument (rotation_error) must be an integer or float. Recieved type %r." % type(rotation_error).__name__

    #Check user input ranges/sizes
    assert states.shape[0] == 3, "In the at_pose function, the dimension of the state of each robot must be 3 ([x;y;theta]). Recieved %r." % states.shape[0]
    assert poses.shape[0] == 3, "In the at_pose function, the dimension of the checked pose of each robot must be 3 ([x;y;theta]). Recieved %r." % poses.shape[0]
    assert states.shape == poses.shape, "In the at_pose function, the robot current state and checked pose inputs must be the same size (3xN, where N is the number of robots being checked). Recieved a state array of size %r x %r and checked pose array of size %r x %r." % (states.shape[0], states.shape[1], poses.shape[0], poses.shape[1])

    # Calculate rotation errors with angle wrapping
    res = states[2, :] - poses[2, :]
    res = np.abs(np.arctan2(np.sin(res), np.cos(res)))

    # Calculate position errors
    pes = np.linalg.norm(states[:2, :] - poses[:2, :], 2, 0)

    # Determine which agents are done
    done = np.nonzero((res <= rotation_error) & (pes <= position_error))

    return done

def create_si_to_uni_dynamics(linear_velocity_gain=1, angular_velocity_limit=np.pi):
    """ Returns a function mapping from single-integrator to unicycle dynamics with angular velocity magnitude restrictions.

        linear_velocity_gain: Gain for unicycle linear velocity
        angular_velocity_limit: Limit for angular velocity (i.e., |w| < angular_velocity_limit)

        -> function
    """

    #Check user input types
    assert isinstance(linear_velocity_gain, (int, float)), "In the function create_si_to_uni_dynamics, the linear velocity gain (linear_velocity_gain) must be an integer or float. Recieved type %r." % type(linear_velocity_gain).__name__
    assert isinstance(angular_velocity_limit, (int, float)), "In the function create_si_to_uni_dynamics, the angular velocity limit (angular_velocity_limit) must be an integer or float. Recieved type %r." % type(angular_velocity_limit).__name__

    #Check user input ranges/sizes
    assert linear_velocity_gain > 0, "In the function create_si_to_uni_dynamics, the linear velocity gain (linear_velocity_gain) must be positive. Recieved %r." % linear_velocity_gain
    assert angular_velocity_limit >= 0, "In the function create_si_to_uni_dynamics, the angular velocity limit (angular_velocity_limit) must not be negative. Recieved %r." % angular_velocity_limit
    

    def si_to_uni_dyn(dxi, poses):
        """A mapping from single-integrator to unicycle dynamics.

        dxi: 2xN numpy array with single-integrator control inputs
        poses: 2xN numpy array with single-integrator poses

        -> 2xN numpy array of unicycle control inputs
        """

        #Check user input types
        assert isinstance(dxi, np.ndarray), "In the si_to_uni_dyn function created by the create_si_to_uni_dynamics function, the single integrator velocity inputs (dxi) must be a numpy array. Recieved type %r." % type(dxi).__name__
        assert isinstance(poses, np.ndarray), "In the si_to_uni_dyn function created by the create_si_to_uni_dynamics function, the current robot poses (poses) must be a numpy array. Recieved type %r." % type(poses).__name__

        #Check user input ranges/sizes
        assert dxi.shape[0] == 2, "In the si_to_uni_dyn function created by the create_si_to_uni_dynamics function, the dimension of the single integrator velocity inputs (dxi) must be 2 ([x_dot;y_dot]). Recieved dimension %r." % dxi.shape[0]
        assert poses.shape[0] == 3, "In the si_to_uni_dyn function created by the create_si_to_uni_dynamics function, the dimension of the current pose of each robot must be 3 ([x;y;theta]). Recieved dimension %r." % poses.shape[0]
        assert dxi.shape[1] == poses.shape[1], "In the si_to_uni_dyn function created by the create_si_to_uni_dynamics function, the number of single integrator velocity inputs must be equal to the number of current robot poses. Recieved a single integrator velocity input array of size %r x %r and current pose array of size %r x %r." % (dxi.shape[0], dxi.shape[1], poses.shape[0], poses.shape[1])

        M,N = np.shape(dxi)

        a = np.cos(poses[2, :])
        b = np.sin(poses[2, :])

        dxu = np.zeros((2, N))
        dxu[0, :] = linear_velocity_gain*(a*dxi[0, :] + b*dxi[1, :])
        dxu[1, :] = angular_velocity_limit*np.arctan2(-b*dxi[0, :] + a*dxi[1, :], dxu[0, :])/(np.pi/2)

        return dxu

    return si_to_uni_dyn

def create_si_to_uni_mapping(projection_distance=0.05, angular_velocity_limit=np.pi):
	"""Creates two functions for mapping from single integrator dynamics to
    unicycle dynamics and unicycle states to single integrator states.

    This mapping is done by placing a virtual control "point" in front of
    the unicycle.

    projection_distance: How far ahead to place the point
    angular_velocity_limit: The maximum angular velocity that can be provided

    -> (function, function)
    """

	# Check user input types
	assert isinstance(projection_distance, (int,
											float)), "In the function create_si_to_uni_mapping, the projection distance of the new control point (projection_distance) must be an integer or float. Recieved type %r." % type(
		projection_distance).__name__
	assert isinstance(angular_velocity_limit, (int,
											   float)), "In the function create_si_to_uni_mapping, the maximum angular velocity command (angular_velocity_limit) must be an integer or float. Recieved type %r." % type(
		angular_velocity_limit).__name__

	# Check user input ranges/sizes
	assert projection_distance > 0, "In the function create_si_to_uni_mapping, the projection distance of the new control point (projection_distance) must be positive. Recieved %r." % projection_distance
	assert projection_distance >= 0, "In the function create_si_to_uni_mapping, the maximum angular velocity command (angular_velocity_limit) must be greater than or equal to zero. Recieved %r." % angular_velocity_limit

	def si_to_uni_dyn(dxi, poses):
		"""Takes single-integrator velocities and transforms them to unicycle
        control inputs.

        dxi: 2xN numpy array of single-integrator control inputs
        poses: 3xN numpy array of unicycle poses

        -> 2xN numpy array of unicycle control inputs
        """

		# Check user input types
		assert isinstance(dxi,
						  np.ndarray), "In the si_to_uni_dyn function created by the create_si_to_uni_mapping function, the single integrator velocity inputs (dxi) must be a numpy array. Recieved type %r." % type(
			dxi).__name__
		assert isinstance(poses,
						  np.ndarray), "In the si_to_uni_dyn function created by the create_si_to_uni_mapping function, the current robot poses (poses) must be a numpy array. Recieved type %r." % type(
			poses).__name__

		# Check user input ranges/sizes
		assert dxi.shape[
				   0] == 2, "In the si_to_uni_dyn function created by the create_si_to_uni_mapping function, the dimension of the single integrator velocity inputs (dxi) must be 2 ([x_dot;y_dot]). Recieved dimension %r." % \
							dxi.shape[0]
		assert poses.shape[
				   0] == 3, "In the si_to_uni_dyn function created by the create_si_to_uni_mapping function, the dimension of the current pose of each robot must be 3 ([x;y;theta]). Recieved dimension %r." % \
							poses.shape[0]
		assert dxi.shape[1] == poses.shape[
			1], "In the si_to_uni_dyn function created by the create_si_to_uni_mapping function, the number of single integrator velocity inputs must be equal to the number of current robot poses. Recieved a single integrator velocity input array of size %r x %r and current pose array of size %r x %r." % (
		dxi.shape[0], dxi.shape[1], poses.shape[0], poses.shape[1])

		M, N = np.shape(dxi)

		cs = np.cos(poses[2, :])
		ss = np.sin(poses[2, :])

		dxu = np.zeros((2, N))
		dxu[0, :] = (cs * dxi[0, :] + ss * dxi[1, :])
		dxu[1, :] = (1 / projection_distance) * (-ss * dxi[0, :] + cs * dxi[1, :])

		# Impose angular velocity cap.
		dxu[1, dxu[1, :] > angular_velocity_limit] = angular_velocity_limit
		dxu[1, dxu[1, :] < -angular_velocity_limit] = -angular_velocity_limit

		return dxu

	def uni_to_si_states(poses):
		"""Takes unicycle states and returns single-integrator states

        poses: 3xN numpy array of unicycle states

        -> 2xN numpy array of single-integrator states
        """

		_, N = np.shape(poses)

		si_states = np.zeros((2, N))
		si_states[0, :] = poses[0, :] + projection_distance * np.cos(poses[2, :])
		si_states[1, :] = poses[1, :] + projection_distance * np.sin(poses[2, :])

		return si_states

	return si_to_uni_dyn, uni_to_si_states


def create_si_position_controller(x_velocity_gain=1, y_velocity_gain=1, velocity_magnitude_limit=0.15):
    """Creates a position controller for single integrators.  Drives a single integrator to a point
    using a propoertional controller.

    x_velocity_gain - the gain impacting the x (horizontal) velocity of the single integrator
    y_velocity_gain - the gain impacting the y (vertical) velocity of the single integrator
    velocity_magnitude_limit - the maximum magnitude of the produce velocity vector (should be less than the max linear speed of the platform)

    -> function
    """

    #Check user input types
    assert isinstance(x_velocity_gain, (int, float)), "In the function create_si_position_controller, the x linear velocity gain (x_velocity_gain) must be an integer or float. Recieved type %r." % type(x_velocity_gain).__name__
    assert isinstance(y_velocity_gain, (int, float)), "In the function create_si_position_controller, the y linear velocity gain (y_velocity_gain) must be an integer or float. Recieved type %r." % type(y_velocity_gain).__name__
    assert isinstance(velocity_magnitude_limit, (int, float)), "In the function create_si_position_controller, the velocity magnitude limit (y_velocity_gain) must be an integer or float. Recieved type %r." % type(y_velocity_gain).__name__
    
    #Check user input ranges/sizes
    assert x_velocity_gain > 0, "In the function create_si_position_controller, the x linear velocity gain (x_velocity_gain) must be positive. Recieved %r." % x_velocity_gain
    assert y_velocity_gain > 0, "In the function create_si_position_controller, the y linear velocity gain (y_velocity_gain) must be positive. Recieved %r." % y_velocity_gain
    assert velocity_magnitude_limit >= 0, "In the function create_si_position_controller, the velocity magnitude limit (velocity_magnitude_limit) must not be negative. Recieved %r." % velocity_magnitude_limit
    
    gain = np.diag([x_velocity_gain, y_velocity_gain])

    def si_position_controller(xi, positions):

        """
        xi: 2xN numpy array (of single-integrator states of the robots)
        points: 2xN numpy array (of desired points each robot should achieve)

        -> 2xN numpy array (of single-integrator control inputs)

        """

        #Check user input types
        assert isinstance(xi, np.ndarray), "In the si_position_controller function created by the create_si_position_controller function, the single-integrator robot states (xi) must be a numpy array. Recieved type %r." % type(xi).__name__
        assert isinstance(positions, np.ndarray), "In the si_position_controller function created by the create_si_position_controller function, the robot goal points (positions) must be a numpy array. Recieved type %r." % type(positions).__name__

        #Check user input ranges/sizes
        assert xi.shape[0] == 2, "In the si_position_controller function created by the create_si_position_controller function, the dimension of the single-integrator robot states (xi) must be 2 ([x;y]). Recieved dimension %r." % xi.shape[0]
        assert positions.shape[0] == 2, "In the si_position_controller function created by the create_si_position_controller function, the dimension of the robot goal points (positions) must be 2 ([x_goal;y_goal]). Recieved dimension %r." % positions.shape[0]
        assert xi.shape[1] == positions.shape[1], "In the si_position_controller function created by the create_si_position_controller function, the number of single-integrator robot states (xi) must be equal to the number of robot goal points (positions). Recieved a single integrator current position input array of size %r x %r and desired position array of size %r x %r." % (xi.shape[0], xi.shape[1], positions.shape[0], positions.shape[1])

        _,N = np.shape(xi)
        dxi = np.zeros((2, N))

        # Calculate control input
        dxi[0][:] = x_velocity_gain*(positions[0][:]-xi[0][:])
        dxi[1][:] = y_velocity_gain*(positions[1][:]-xi[1][:])

        # Threshold magnitude
        norms = np.linalg.norm(dxi, axis=0)
        idxs = np.where(norms > velocity_magnitude_limit)
        if norms[idxs].size != 0:
            dxi[:, idxs] *= velocity_magnitude_limit/norms[idxs]

        return dxi

    return si_position_controller


def de_create_single_integrator_CLF_CBF_CBF3(barrier_gain=1, safety_radius=0.17, magnitude_limit=0.2):
    """Creates a barrier certificate for a single-integrator system.  This function
    returns another function for optimization reasons.

    barrier_gain: double (controls how quickly agents can approach each other.  lower = slower)
    safety_radius: double (how far apart the agents will stay)
    magnitude_limit: how fast the robot can move linearly.

    -> function (the barrier certificate function)
    """
    epi = 0.1
    lambda1 = 1
    lambda2 = 1
    MM_clf = np.array([[lambda1, 0], [0, lambda2]])

    def sigmoid2(d):
        # z = 1. / (1. + np.exp(-(d - 0.)))
        z = 1. / (1. + np.exp(-10. * (d - 1600.)))
        # z = 0.
        return z

    def f(x, xo, xgoal, omega, uui, uuo):
        # Initialize some variables for computational savings
        num_obstacles = xo.shape[1]
        num_constraints = num_obstacles * 2 + 1
        A = np.zeros((num_constraints, 4))
        b = np.zeros(num_constraints)
        Q0 = np.array([[math.cos(omega), -math.sin(omega)], [math.sin(omega), math.cos(omega)]])
        OX = np.array([[-x[1, 0]], [x[0, 0]]])
        deltaV = 2 * Q0.T @ MM_clf @ (Q0 @ x - xgoal)
        deltaQV = OX.T @ deltaV
        riski = 0
        # H = sparse(matrix(2 * np.identity(3)))

        for i in range(1, num_obstacles+1):
            error = x[:, 0] - xo[:, i-1]
            h_x = (error[0] * error[0] + error[1] * error[1]) - np.power(safety_radius, 2)
            if h_x <= 0:
                print(h_x)
            A[i, 0:2] = -error.T
            b[i] = 0.5 * barrier_gain * h_x


            ## CBF2
            sigma_x = math.exp(-(h_x ** 2))
            deltaH = 2 * np.array([[error[0]], [error[1]]])

            uuerror = np.array([[(uui[:, 0] - uuo[:, i - 1])[0]], [(uui[:, 0] - uuo[:, i - 1])[1]]])
            riski += deltaH.T @ uuerror + barrier_gain * h_x

            PdeltaH = np.linalg.norm(deltaH) * np.eye(2) - deltaH @ deltaH.T

            PdeltaV = np.linalg.norm(deltaV) * np.eye(2) - deltaV @ deltaV.T

            HV = 2 * Q0.T @ Q0
            Hh = 2 * np.array([[1, 0], [0, 1]])
            deltaD = HV @ PdeltaH @ deltaV + Hh @ PdeltaV @ deltaH
            DD = 0.5 * deltaV.T @ PdeltaH @ deltaV
            deltaHD = sigma_x * deltaD - 2 * h_x * sigma_x * (DD - epi) * deltaH
            deltaQD = (HV @ OX - np.array([[-deltaV[1, 0]], [deltaV[0, 0]]])).T @ PdeltaH @ deltaV
            delta_QHD = sigma_x * deltaQD
            HD = sigma_x * (DD - epi)
            A[i + num_obstacles, 0:2] = -deltaHD.T
            A[i + num_obstacles, 3] = -delta_QHD.T
            b[i + num_obstacles] = HD

        # clf
        A[0, 0:2] = deltaV.T  # for u
        A[0, 2] = -1  # for delta
        A[0, 3] = deltaQV[0].T  # for omega
        b[0] = -(Q0 @ x - xgoal).T @ MM_clf @ (Q0 @ x - xgoal)

        # norms = np.linalg.norm(dxi, 2, 0)
        # idxs_to_normalize = (norms > magnitude_limit)
        # dxi[:, idxs_to_normalize] *= magnitude_limit / norms[idxs_to_normalize]

        f = np.zeros((4, 1))
        H = np.array([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
        H = sparse.csc_matrix(H)
        A = sparse.csc_matrix(A)
        result = solve_qp(H, f, A, b, solver='osqp', max_iter = 5000, initvals=np.array([0., 0., 0., math.pi / 2.]),eps_prim_inf=1e-7)

        return result

    return f





_, uni_to_si_states = create_si_to_uni_mapping()

si_to_uni_dyn = create_si_to_uni_dynamics()
single_integrator_position_controller = create_si_position_controller()
si_barrier_cert = de_create_single_integrator_CLF_CBF_CBF3(safety_radius=4.0)
barrier_gain_CBF = 1
safety_radius = 4.0
magnitude_limit = 2
epi = 0.1
lambda1 = 1
lambda2 = 1
MM_clf = np.array([[lambda1, 0], [0, lambda2]])
riskivalue = []
N = 4
aa = np.pi

Omega = math.pi / 2 * np.ones((N))

def de_CLF_CBF(x, xo, xgoal, omega, uui, uuo, riskmatrixi, riskmatrixo):
    # print(omega)
    # Initialize some variables for computational savings
    # print(omega)
    num_obstacles = xo.shape[1]
    num_constraints = num_obstacles * 2 + 1
    A = np.zeros((num_constraints, 4))
    b = np.zeros(num_constraints)
    # H = sparse(matrix(2 * np.identity(3)))
    Q0 = np.array([[math.cos(omega), -math.sin(omega)], [math.sin(omega), math.cos(omega)]])
    # Q0 = np.array([[math.cos(0), -math.sin(0)], [math.sin(0), math.cos(0)]])
    ## CLF
    ## V(x) = |Q@x - xgoal|**2
    OX = np.array([[-x[1, 0]], [x[0, 0]]])
    deltaV = 2 * Q0.T @ MM_clf @ (Q0 @ x - xgoal)
    deltaQV = OX.T @ deltaV
    riski = 0

    for i in range(1, num_obstacles + 1):
        ## CBF1
        error = x[:, 0] - xo[:, i - 1]
        h_x = (error[0] * error[0] + error[1] * error[1]) - np.power(safety_radius, 2)
        if h_x <= 0:
            print(x, xo)
            print(i, h_x)
        A[i, 0:2] = -error.T
        ratio = 1 - (riskmatrixi / (riskmatrixi + riskmatrixo[i-1]))
        b[i] = ratio * barrier_gain_CBF * h_x
        deltaH = 2 * np.array([[error[0]], [error[1]]])
        uuerror = np.array([[(uui[:, 0] - uuo[:, i - 1])[0]], [(uui[:, 0] - uuo[:, i - 1])[1]]])
        riski += deltaH.T @ uuerror + barrier_gain_CBF * h_x

    riski = -riski + 6000
    riskvalue = riski / (N - 1)

    for i in range(1, num_obstacles + 1):
        error = x[:, 0] - xo[:, i - 1]
        h_x = (error[0] * error[0] + error[1] * error[1]) - np.power(safety_radius, 2)

        ## CBF2
        sigma_x = math.exp(-(h_x ** 2))
        deltaH = 2 * np.array([[error[0]], [error[1]]])

        PdeltaH = np.linalg.norm(deltaH) * np.eye(2) - deltaH @ deltaH.T
        PdeltaV = np.linalg.norm(deltaV) * np.eye(2) - deltaV @ deltaV.T

        HV = 2 * Q0.T @ Q0
        Hh = 2 * np.array([[1, 0], [0, 1]])
        deltaD = HV @ PdeltaH @ deltaV + Hh @ PdeltaV @ deltaH
        DD = 0.5 * deltaV.T @ PdeltaH @ deltaV

        deltaHD = sigma_x * deltaD - 2 * h_x * sigma_x * (DD - epi) * deltaH
        deltaQD = (HV @ OX - np.array([[-deltaV[1, 0]], [deltaV[0, 0]]])).T @ PdeltaH @ deltaV
        delta_QHD = sigma_x * deltaQD
        HD = sigma_x * (DD - epi)
        A[i + num_obstacles, 0:2] = -(sigmoid2(riskvalue)) * deltaHD.T
        A[i + num_obstacles, 3] = -(sigmoid2(riskvalue)) * delta_QHD.T
        # A[i + num_obstacles, 0:2] = - deltaHD.T
        # A[i + num_obstacles, 3] = - delta_QHD.T
        # b[i + num_obstacles] = HD
        b[i + num_obstacles] = HD

    # norms = np.linalg.norm(dxi, 2, 0)
    # idxs_to_normalize = (norms > magnitude_limit)
    # dxi[:, idxs_to_normalize] *= magnitude_limit / norms[idxs_to_normalize]

    # A[0, 0:2] = deltaV.T  # for u
    print(riskvalue)
    riskivalue.append(riskvalue)
    deltaV_2 = 2 * MM_clf @ (x - xgoal)

    A[0, 0:2] = ((sigmoid2(riskvalue)) * deltaV + (1 - sigmoid2(riskvalue)) * deltaV_2).T
    A[0, 2] = -1  # for delta
    A[0, 3] = deltaQV[0].T  # for omega
    b[0] = -(Q0 @ x - xgoal).T @ MM_clf @ (Q0 @ x - xgoal)

    f = np.zeros((4, 1))
    H = np.array([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 10]])
    H = sparse.csc_matrix(H)
    A = sparse.csc_matrix(A)
    # result = solve_qp(H, f, A, b, lb=np.array([-1., -1., -math.inf, -math.pi / 2]),
    #                   ub=np.array([1., 1., math.inf, math.pi / 2]), solver='osqp', max_iter=4000, verbose=True)
    result = solve_qp(H, f, A, b, solver='osqp', max_iter=6000, eps_prim_inf=1e-9,
                      lb=np.array([-math.inf, -math.inf, -math.inf, -math.pi/2]),
                      ub=np.array([math.inf, math.inf, math.inf, math.pi/2]),
    initvals = np.array([0., 0., 0., math.pi / 2]), verbose = True)
    return result

def sigmoid2(d):
    # z = 1. / (1. + np.exp(-(d - 0.)))
    # z = 1. / (1. + np.exp(-10. * (d - 701.)))
    z = 1. / (1. + np.exp(-10. * (d -1960.)))
    # z = 1. / (1. + np.exp(-10. * (d - 800.)))
    # z = 1. / (1. + np.exp(-10. * (d - 800.)))
    # z = 1.
    return z

def riskMatixCal(x, uu):
    riskmatrix = np.zeros(N)
    for i in range(N):
        xi = x[2 * i:2 * i + 2]
        xi = xi.reshape((2, -1))
        indices_to_eliminate = [2 * i, 2 * i + 1]
        xo = np.delete(x, indices_to_eliminate)
        xo = xo.reshape((2, -1), order='F')

        uui = uu[2 * i:2 * i + 2, 0]
        uui = uui.reshape((2, -1))
        uuo = np.delete(uu, indices_to_eliminate)
        uuo = uuo.reshape((2, -1), order='F')
        riski = riskiCal(xi, xo, uui, uuo)
        riskmatrix[i] = riski
    return riskmatrix



def riskiCal(xi, xo, uui, uuo):
    riski = 0
    num_obstacles = xo.shape[1]
    for i in range(1, num_obstacles + 1):
        error = xi[:, 0] - xo[:, i - 1]
        h_x = (error[0] * error[0] + error[1] * error[1]) - np.power(safety_radius, 2)
        deltaH = 2 * np.array([[error[0]], [error[1]]])
        uuerror = np.array([[(uui[:, 0] - uuo[:, i - 1])[0]], [(uui[:, 0] - uuo[:, i - 1])[1]]])
        riski += deltaH.T @ uuerror + barrier_gain_CBF * h_x
    riski = -riski + 6000
    riski = riski / (N-1)
    return riski


initial_conditions = np.array([[0., 0., -1., 1.], [1., -1., 0., 0.], [-math.pi / 2, math.pi / 2, 0., math.pi]])
goal_points = np.array([[0., 0., 1., -1.], [-1., 1., 0., 0.], [math.pi / 2, -math.pi / 2, math.pi, 0.]])
x = np.array([[0.0,0.5,-0.5,1.0],[0.0,-0.5,0.5,-1.0],[0.2,0.2,0.2,0.2]])
x_si = uni_to_si_states(x)
Omega = math.pi / 2 * np.ones((N))
dxu = np.zeros((2, N))


rospy.init_node('teleop_twist_keyboard')
publisher = rospy.Publisher('/cmd_vel', Twist, queue_size = 1)
rospy.sleep(2)
twist = Twist()


def callback(data, args):

	i = args

	theta = tf_conversions.transformations.euler_from_quaternion([data.pose.orientation.x, data.pose.orientation.y, data.pose.orientation.z, data.pose.orientation.w])[2]
	x[0,i] = data.pose.position.x
	x[1,i] = data.pose.position.y
	x[2,i] = theta


dxu = np.zeros((2, N))
uu = np.array([[dxu[0,0]],[dxu[1,0]],[dxu[0,1]],[dxu[1,1]],[dxu[0,2]],[dxu[1,2]],[dxu[0,3]],[dxu[1,3]]])
def control_callback(event):
	#set p according to your robot index
	p = 3
    
	for i in range(N):
		x_si = uni_to_si_states(x)
		xx_si = np.array([x_si[0,0], x_si[1,0], x_si[0,1], x_si[1,1], x_si[0,2], x_si[1,2], x_si[0,3], x_si[1,3]])

		riskmatrix = riskMatixCal(xx_si, uu)
		riskmatrixi = riskmatrix[i]
		indices_to_eliminate = [i]
		riskmatrixo = np.delete(riskmatrix, indices_to_eliminate)


		# robot i
		xx = np.reshape(x[:, i], (3, 1))
		xi = np.reshape(x_si[:, i], (2, 1))
		mask = np.arange(x_si.shape[1]) != i
		xo = x_si[:, mask]  # for obstacles
		xgoal = goal_points[0:2, i].reshape((2, -1))
		indices_to_eliminate = [2 * i, 2 * i + 1]
		uui = uu[2 * i:2 * i + 2, 0]
		uui = uui.reshape((2, -1))
		uuo = np.delete(uu, indices_to_eliminate)
		uuo = uuo.reshape((2, -1), order='F')
		# print(np.vstack((x_si, x[2, :])))
		if np.size(at_pose(np.vstack((xi, x[2, i])), np.vstack((xgoal, goal_points[2, i])), position_error=0.3, rotation_error=100))!=1:
			dxx = de_CLF_CBF(xi*10, xo*10, xgoal*10, Omega[i], uui, uuo, riskmatrixi, riskmatrixo)
			# dxx = si_barrier_cert(xi * 10, xo * 10, xgoal * 10, Omega[i])
		else:
			dxx = single_integrator_position_controller(xi, xgoal)
			dxx = np.array([dxx[0,0], dxx[1,0], 0., math.pi / 2])
			# dx = dxx

		if dxx is None:
			dxx = [0, 0, 0, math.pi / 2]
			print(i)

		Omega[i] = dxx[3]
		dx = np.array([[dxx[0]], [dxx[1]]])
		du = si_to_uni_dyn(dx, xx)

		dxu[0, i] = du[0, 0]
		dxu[1, i] = du[1, 0]

	twist.linear.x = dxu[0,p]/50.
	twist.linear.y = 0.0
	twist.linear.z = 0.0
	twist.angular.x = 0
	twist.angular.y = 0
	twist.angular.z = dxu[1,p]/25.
	publisher.publish(twist)

def central():

	
	rospy.Subscriber('/vrpn_client_node/Hus117'  + '/pose', PoseStamped, callback, 0 ) 
	rospy.Subscriber('/vrpn_client_node/Hus137'  + '/pose', PoseStamped, callback, 1 ) 
	rospy.Subscriber('/vrpn_client_node/Hus138'  + '/pose', PoseStamped, callback, 2 ) 
	rospy.Subscriber('/vrpn_client_node/Hus188'  + '/pose', PoseStamped, callback, 3 ) 

	
	timer = rospy.Timer(rospy.Duration(0.05), control_callback)
	rospy.spin()


if __name__ == '__main__':

	try:
		central()
	except rospy.ROSInterruptException:
		print(rospy.ROSInterruptException)
