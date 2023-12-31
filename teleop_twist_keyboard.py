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
from cvxopt import matrix, sparse

import qpsolvers
from qpsolvers import solve_qp
from scipy import sparse as sparsed
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


def create_uni_to_si_dynamics(projection_distance=0.05):
	"""Creates two functions for mapping from unicycle dynamics to single
    integrator dynamics and single integrator states to unicycle states.

    This mapping is done by placing a virtual control "point" in front of
    the unicycle.

    projection_distance: How far ahead to place the point

    -> function
    """

	# Check user input types
	assert isinstance(projection_distance, (int,
											float)), "In the function create_uni_to_si_dynamics, the projection distance of the new control point (projection_distance) must be an integer or float. Recieved type %r." % type(
		projection_distance).__name__

	# Check user input ranges/sizes
	assert projection_distance > 0, "In the function create_uni_to_si_dynamics, the projection distance of the new control point (projection_distance) must be positive. Recieved %r." % projection_distance

	def uni_to_si_dyn(dxu, poses):
		"""A function for converting from unicycle to single-integrator dynamics.
        Utilizes a virtual point placed in front of the unicycle.

        dxu: 2xN numpy array of unicycle control inputs
        poses: 3xN numpy array of unicycle poses
        projection_distance: How far ahead of the unicycle model to place the point

        -> 2xN numpy array of single-integrator control inputs
        """

		# Check user input types

		assert isinstance(poses,
						  np.ndarray), "In the uni_to_si_dyn function created by the create_uni_to_si_dynamics function, the current robot poses (poses) must be a numpy array. Recieved type %r." % type(
			poses).__name__

		# Check user input ranges/sizes
		assert dxu.shape[
				   0] == 2, "In the uni_to_si_dyn function created by the create_uni_to_si_dynamics function, the dimension of the unicycle velocity inputs (dxu) must be 2 ([v;w]). Recieved dimension %r." % \
							dxu.shape[0]
		assert poses.shape[
				   0] == 3, "In the uni_to_si_dyn function created by the create_uni_to_si_dynamics function, the dimension of the current pose of each robot must be 3 ([x;y;theta]). Recieved dimension %r." % \
							poses.shape[0]
		assert dxu.shape[1] == poses.shape[
			1], "In the uni_to_si_dyn function created by the create_uni_to_si_dynamics function, the number of unicycle velocity inputs must be equal to the number of current robot poses. Recieved a unicycle velocity input array of size %r x %r and current pose array of size %r x %r." % (
		dxu.shape[0], dxu.shape[1], poses.shape[0], poses.shape[1])

		M, N = np.shape(dxu)

		cs = np.cos(poses[2, :])
		ss = np.sin(poses[2, :])

		dxi = np.zeros((2, N))
		dxi[0, :] = (cs * dxu[0, :] - projection_distance * ss * dxu[1, :])
		dxi[1, :] = (ss * dxu[0, :] + projection_distance * cs * dxu[1, :])

		return dxi

	return uni_to_si_dyn



def create_single_integrator_barrier_certificate(barrier_gain=100, safety_radius=0.17, magnitude_limit=100):
	"""Creates a barrier certificate for a single-integrator system.  This function
    returns another function for optimization reasons.

    barrier_gain: double (controls how quickly agents can approach each other.  lower = slower)
    safety_radius: double (how far apart the agents will stay)
    magnitude_limit: how fast the robot can move linearly.

    -> function (the barrier certificate function)
    """

	# Check user input types
	assert isinstance(barrier_gain, (int,
									 float)), "In the function create_single_integrator_barrier_certificate, the barrier gain (barrier_gain) must be an integer or float. Recieved type %r." % type(
		barrier_gain).__name__
	assert isinstance(safety_radius, (int,
									  float)), "In the function create_single_integrator_barrier_certificate, the safe distance between robots (safety_radius) must be an integer or float. Recieved type %r." % type(
		safety_radius).__name__
	assert isinstance(magnitude_limit, (int,
										float)), "In the function create_single_integrator_barrier_certificate, the maximum linear velocity of the robot (magnitude_limit) must be an integer or float. Recieved type %r." % type(
		magnitude_limit).__name__

	# Check user input ranges/sizes
	assert barrier_gain > 0, "In the function create_single_integrator_barrier_certificate, the barrier gain (barrier_gain) must be positive. Recieved %r." % barrier_gain
	assert safety_radius >= 0.12, "In the function create_single_integrator_barrier_certificate, the safe distance between robots (safety_radius) must be greater than or equal to the diameter of the robot (0.12m) plus the distance to the look ahead point used in the diffeomorphism if that is being used. Recieved %r." % safety_radius
	assert magnitude_limit > 0, "In the function create_single_integrator_barrier_certificate, the maximum linear velocity of the robot (magnitude_limit) must be positive. Recieved %r." % magnitude_limit
	#assert magnitude_limit <= 0.2, "In the function create_single_integrator_barrier_certificate, the maximum linear velocity of the robot (magnitude_limit) must be less than the max speed of the robot (0.2m/s). Recieved %r." % magnitude_limit

	def f(dxi, x):
		# Check user input types
		assert isinstance(dxi,
						  np.ndarray), "In the function created by the create_single_integrator_barrier_certificate function, the single-integrator robot velocity command (dxi) must be a numpy array. Recieved type %r." % type(
			dxi).__name__
		assert isinstance(x,
						  np.ndarray), "In the function created by the create_single_integrator_barrier_certificate function, the robot states (x) must be a numpy array. Recieved type %r." % type(
			x).__name__

		# Check user input ranges/sizes
		assert x.shape[
				   0] == 2, "In the function created by the create_single_integrator_barrier_certificate function, the dimension of the single integrator robot states (x) must be 2 ([x;y]). Recieved dimension %r." % \
							x.shape[0]
		assert dxi.shape[
				   0] == 2, "In the function created by the create_single_integrator_barrier_certificate function, the dimension of the robot single integrator velocity command (dxi) must be 2 ([x_dot;y_dot]). Recieved dimension %r." % \
							dxi.shape[0]
		assert x.shape[1] == dxi.shape[
			1], "In the function created by the create_single_integrator_barrier_certificate function, the number of robot states (x) must be equal to the number of robot single integrator velocity commands (dxi). Recieved a current robot pose input array (x) of size %r x %r and single integrator velocity array (dxi) of size %r x %r." % (

		x.shape[0], x.shape[1], dxi.shape[0], dxi.shape[1])

		# Initialize some variables for computational savings
		N = dxi.shape[1]
		num_constraints = int(comb(N, 2))
		A = np.zeros((num_constraints, 2 * N))
		b = np.zeros(num_constraints)
		H = sparse(matrix(2 * np.identity(2 * N)))

		count = 0
		for i in range(N-1):
			for j in range(i + 1, N):
				error = x[:, i] - x[:, j]
				h = (error[0] * error[0] + error[1] * error[1]) - np.power(safety_radius, 2)

				A[count, (2 * i, (2 * i + 1))] = -2 * error
				A[count, (2 * j, (2 * j + 1))] = 2 * error
				b[count] = barrier_gain * np.power(h, 3)

				count += 1

		# Threshold control inputs before QP
		norms = np.linalg.norm(dxi, 2, 0)
		idxs_to_normalize = (norms > magnitude_limit)
		dxi[:, idxs_to_normalize] *= magnitude_limit / norms[idxs_to_normalize]

		f = -2 * np.reshape(dxi, 2 * N, order='F')
		result = qp(H, matrix(f), matrix(A), matrix(b))['x']

		return np.reshape(result, (2, -1), order='F')

	return f

def create_unicycle_barrier_certificate(barrier_gain=100, safety_radius=0.12, projection_distance=0.05, magnitude_limit=100):
    """ Creates a unicycle barrier cetifcate to avoid collisions. Uses the diffeomorphism mapping
    and single integrator implementation. For optimization purposes, this function returns
    another function.

    barrier_gain: double (how fast the robots can approach each other)
    safety_radius: double (how far apart the robots should stay)
    projection_distance: double (how far ahead to place the bubble)

    -> function (the unicycle barrier certificate function)
    """

    #Check user input types
    assert isinstance(barrier_gain, (int, float)), "In the function create_unicycle_barrier_certificate, the barrier gain (barrier_gain) must be an integer or float. Recieved type %r." % type(barrier_gain).__name__
    assert isinstance(safety_radius, (int, float)), "In the function create_unicycle_barrier_certificate, the safe distance between robots (safety_radius) must be an integer or float. Recieved type %r." % type(safety_radius).__name__
    assert isinstance(projection_distance, (int, float)), "In the function create_unicycle_barrier_certificate, the projected point distance for the diffeomorphism between sinlge integrator and unicycle (projection_distance) must be an integer or float. Recieved type %r." % type(projection_distance).__name__
    assert isinstance(magnitude_limit, (int, float)), "In the function create_unicycle_barrier_certificate, the maximum linear velocity of the robot (magnitude_limit) must be an integer or float. Recieved type %r." % type(magnitude_limit).__name__

    #Check user input ranges/sizes
    assert barrier_gain > 0, "In the function create_unicycle_barrier_certificate, the barrier gain (barrier_gain) must be positive. Recieved %r." % barrier_gain
    assert safety_radius >= 0.12, "In the function create_unicycle_barrier_certificate, the safe distance between robots (safety_radius) must be greater than or equal to the diameter of the robot (0.12m). Recieved %r." % safety_radius
    assert projection_distance > 0, "In the function create_unicycle_barrier_certificate, the projected point distance for the diffeomorphism between sinlge integrator and unicycle (projection_distance) must be positive. Recieved %r." % projection_distance
    assert magnitude_limit > 0, "In the function create_unicycle_barrier_certificate, the maximum linear velocity of the robot (magnitude_limit) must be positive. Recieved %r." % magnitude_limit
   # assert magnitude_limit <= 0.2, "In the function create_unicycle_barrier_certificate, the maximum linear velocity of the robot (magnitude_limit) must be less than the max speed of the robot (0.2m/s). Recieved %r." % magnitude_limit


    si_barrier_cert = create_single_integrator_barrier_certificate(barrier_gain=barrier_gain, safety_radius=safety_radius+projection_distance)

    si_to_uni_dyn, uni_to_si_states = create_si_to_uni_mapping(projection_distance=projection_distance)

    uni_to_si_dyn = create_uni_to_si_dynamics(projection_distance=projection_distance)

    def f(dxu, x):
        #Check user input types

        assert isinstance(dxu, np.ndarray), "In the function created by the create_unicycle_barrier_certificate function, the unicycle robot velocity command (dxu) must be a numpy array. Recieved type %r." % type(dxu).__name__
        assert isinstance(x, np.ndarray), "In the function created by the create_unicycle_barrier_certificate function, the robot states (x) must be a numpy array. Recieved type %r." % type(x).__name__

        #Check user input ranges/sizes
        assert x.shape[0] == 3, "In the function created by the create_unicycle_barrier_certificate function, the dimension of the unicycle robot states (x) must be 3 ([x;y;theta]). Recieved dimension %r." % x.shape[0]
        assert dxu.shape[0] == 2, "In the function created by the create_unicycle_barrier_certificate function, the dimension of the robot unicycle velocity command (dxu) must be 2 ([v;w]). Recieved dimension %r." % dxu.shape[0]
        assert x.shape[1] == dxu.shape[1], "In the function created by the create_unicycle_barrier_certificate function, the number of robot states (x) must be equal to the number of robot unicycle velocity commands (dxu). Recieved a current robot pose input array (x) of size %r x %r and single integrator velocity array (dxi) of size %r x %r." % (x.shape[0], x.shape[1], dxu.shape[0], dxu.shape[1])


        x_si = uni_to_si_states(x)
        #Convert unicycle control command to single integrator one
        dxi = uni_to_si_dyn(dxu, x)
        #Apply single integrator barrier certificate
        dxi = si_barrier_cert(dxi, x_si)
        #Return safe unicycle command
        return si_to_uni_dyn(dxi, x)

    return f

def de_create_single_integrator_barrier_certificate(barrier_gain=10, safety_radius=0.17, magnitude_limit=0.2):
    """Creates a barrier certificate for a single-integrator system.  This function
    returns another function for optimization reasons.

    barrier_gain: double (controls how quickly agents can approach each other.  lower = slower)
    safety_radius: double (how far apart the agents will stay)
    magnitude_limit: how fast the robot can move linearly.

    -> function (the barrier certificate function)
    """

    def f(dxi, x, xo):

        # Initialize some variables for computational savings
        num_constraints = xo.shape[1]
        A = np.zeros((num_constraints, 2))
        b = np.zeros(num_constraints)
        H = sparse(matrix(2 * np.identity(2)))

        for i in range(num_constraints):
            error = x[:,0] - xo[:, i]
            h = (error[0] * error[0] + error[1] * error[1]) - np.power(safety_radius, 2)
            if h <= 0:
                print(h)
            A[i, :] = -error.T
            b[i] = 0.5 * barrier_gain * h
        norms = np.linalg.norm(dxi, 2, 0)
        idxs_to_normalize = (norms > magnitude_limit)
        dxi[:, idxs_to_normalize] *= magnitude_limit / norms[idxs_to_normalize]

        f = -2 * np.reshape(dxi, 2, order='F')
        H = 0.5*(H+H.T)
        try:
            result = qp(H, matrix(f), matrix(A), matrix(b))['x']
            return np.reshape(result, (2, -1), order='F')
        except:
            return np.array([[0],[0]])


    return f


def create_si_position_controller(x_velocity_gain=1, y_velocity_gain=1, velocity_magnitude_limit=0.15):
	"""Creates a position controller for single integrators.  Drives a single integrator to a point
twist.angular.z = dxu[1][0]
    using a propoertional controller.

    x_velocity_gain - the gain impacting the x (horizontal) velocity of the single integrator
    y_velocity_gain - the gain impacting the y (vertical) velocity of the single integrator
    velocity_magnitude_limit - the maximum magnitude of the produce velocity vector (should be less than the max linear speed of the platform)

    -> function
    """

	# Check user input types
	assert isinstance(x_velocity_gain, (int,
										float)), "In the function create_si_position_controller, the x linear velocity gain (x_velocity_gain) must be an integer or float. Recieved type %r." % type(
		x_velocity_gain).__name__
	assert isinstance(y_velocity_gain, (int,
										float)), "In the function create_si_position_controller, the y linear velocity gain (y_velocity_gain) must be an integer or float. Recieved type %r." % type(
		y_velocity_gain).__name__
	assert isinstance(velocity_magnitude_limit, (int,
												 float)), "In the function create_si_position_controller, the velocity magnitude limit (y_velocity_gain) must be an integer or float. Recieved type %r." % type(
		y_velocity_gain).__name__

	# Check user input ranges/sizes
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

		# Check user input types
		assert isinstance(xi,
						  np.ndarray), "In the si_position_controller function created by the create_si_position_controller function, the single-integrator robot states (xi) must be a numpy array. Recieved type %r." % type(
			xi).__name__
		assert isinstance(positions,
						  np.ndarray), "In the si_position_controller function created by the create_si_position_controller function, the robot goal points (positions) must be a numpy array. Recieved type %r." % type(
			positions).__name__


		# Check user input ranges/sizes
		assert xi.shape[
				   0] == 2, "In the si_position_controller function created by the create_si_position_controller function, the dimension of the single-integrator robot states (xi) must be 2 ([x;y]). Recieved dimension %r." % \
							xi.shape[0]
		assert positions.shape[
				   0] == 2, "In the si_position_controller function created by the create_si_position_controller function, the dimension of the robot goal points (positions) must be 2 ([x_goal;y_goal]). Recieved dimension %r." % \
							positions.shape[0]
		assert xi.shape[1] == positions.shape[
			1], "In the si_position_controller function created by the create_si_position_controller function, the number of single-integrator robot states (xi) must be equal to the number of robot goal points (positions). Recieved a single integrator current position input array of size %r x %r and desired position array of size %r x %r." % (
		xi.shape[0], xi.shape[1], positions.shape[0], positions.shape[1])

		_, N = np.shape(xi)
		dxi = np.zeros((2, N))

		# Calculate control input
		dxi[0][:] = x_velocity_gain * (positions[0][:] - xi[0][:])
		dxi[1][:] = y_velocity_gain * (positions[1][:] - xi[1][:])

		# Threshold magnitude
		norms = np.linalg.norm(dxi, axis=0)
		idxs = np.where(norms > velocity_magnitude_limit)
		if norms[idxs].size != 0:
			dxi[:, idxs] *= velocity_magnitude_limit / norms[idxs]

		return dxi

	return si_position_controller


def create_clf_unicycle_pose_controller(approach_angle_gain=1, desired_angle_gain=2.7, rotation_error_gain=0.3):
	"""Returns a controller ($u: \mathbf{R}^{3 \times N} \times \mathbf{R}^{3 \times N} \to \mathbf{R}^{2 \times N}$)
    that will drive a unicycle-modeled agent to a pose (i.e., position & orientation). This control is based on a control
    Lyapunov function.

    approach_angle_gain - affects how the unicycle approaches the desired position
    desired_angle_gain - affects how the unicycle approaches the desired angle
    rotation_error_gain - affects how quickly the unicycle corrects rotation errors.


    -> function
    """

	gamma = approach_angle_gain
	k = desired_angle_gain
	h = rotation_error_gain

	def R(theta):
		return np.array([[np.cos(theta), -np.sin(theta)], [np.sin(theta), np.cos(theta)]])

	def pose_uni_clf_controller(states, poses):
		N_states = states.shape[1]
		dxu = np.zeros((2, N_states))

		for i in range(N_states):
			translate = R(-poses[2, i]).dot((poses[:2, i] - states[:2, i]))
			e = np.linalg.norm(translate)
			theta = np.arctan2(translate[1], translate[0])
			alpha = theta - (states[2, i] - poses[2, i])
			alpha = np.arctan2(np.sin(alpha), np.cos(alpha))

			ca = np.cos(alpha)
			sa = np.sin(alpha)

			print(gamma)
			print(e)
			print(ca)

			dxu[0, i] = gamma * e * ca
			dxu[1, i] = k * alpha + gamma * ((ca * sa) / alpha) * (alpha + h * theta)

		return dxu

	return pose_uni_clf_controller


def de_create_single_integrator_CLF_CBF(barrier_gain=10, safety_radius=0.17, magnitude_limit=0.2):
    """Creates a barrier certificate for a single-integrator system.  This function
    returns another function for optimization reasons.

    barrier_gain: double (controls how quickly agents can approach each other.  lower = slower)
    safety_radius: double (how far apart the agents will stay)
    magnitude_limit: how fast the robot can move linearly.

    -> function (the barrier certificate function)
    """

    def f(x, xo, xgoal):
        # Initialize some variables for computational savings
        num_constraints = xo.shape[1] + 1
        A = np.zeros((num_constraints, 3))
        b = np.zeros(num_constraints)
        # H = sparse(matrix(2 * np.identity(3)))

        for i in range(num_constraints - 1):
            error = x[:, 0] - xo[:, i]
            h = (error[0] * error[0] + error[1] * error[1]) - np.power(safety_radius, 2)
            if h <= 0:
                print(h)
            A[i, 0:2] = -error.T
            b[i] = 0.5 * barrier_gain * h

        A[num_constraints - 1, 0:2] = 2 * (x - xgoal).T
        A[num_constraints - 1, 2] = -1
        b[num_constraints - 1] = -(x - xgoal).T @ (x - xgoal)

        # norms = np.linalg.norm(dxi, 2, 0)
        # idxs_to_normalize = (norms > magnitude_limit)
        # dxi[:, idxs_to_normalize] *= magnitude_limit / norms[idxs_to_normalize]

        f = np.zeros((3, 1))
        H = np.eye(3)
        H = sparsed.csc_matrix(H)
        A = sparsed.csc_matrix(A)
        result = solve_qp(H, f, A, b, solver='osqp')

        return result

    return f

rospy.init_node('teleop_twist_keyboard')
publisher = rospy.Publisher('/cmd_vel', Twist, queue_size = 1)
rospy.sleep(2)
twist = Twist()
#rate = rospy.Rate(1)

single_integrator_position_controller = create_si_position_controller()

si_barrier_cert = de_create_single_integrator_CLF_CBF(safety_radius=4)
_, uni_to_si_states = create_si_to_uni_mapping()

si_to_uni_dyn = create_si_to_uni_dynamics()
unicycle_position_controller = create_clf_unicycle_pose_controller()
uni_barrier_cert = create_unicycle_barrier_certificate(safety_radius = 0.4)

N = 4
x = np.array([[0.0,0.5,-0.5,1.0],[0.0,-0.5,0.5,-1.0],[0.2,0.2,0.2,0.2]])
dxi = np.array([[0,0,0,0],[0,0,0,0]])
initial_conditions = np.array([[0., 0., -1., 1.], [1., -1., 0., 0.], [-math.pi / 2, math.pi / 2, 0., math.pi]])
goal_points = np.array([[0., 0., 1., -1.], [-1., 1., 0., 0.], [math.pi / 2, -math.pi / 2, math.pi, 0.]])
ready = np.array([0,0,0,0])
def callback(data, args):

	i = args

	theta = tf_conversions.transformations.euler_from_quaternion([data.pose.orientation.x, data.pose.orientation.y, data.pose.orientation.z, data.pose.orientation.w])[2]
	x[0,i] = data.pose.position.x
	x[1,i] = data.pose.position.y
	x[2,i] = theta

def control_callback(event):
	N = 4

	#p for your controlling robot's index
	p = 3
	if ready[0] != 1 or ready[1] != 1 or ready[2] != 1 or ready[3] != 1:

		for i in range(N):
			d = np.sqrt((initial_conditions[0][i] - x[0][i]) ** 2 + (initial_conditions[1][i] - x[1][i]) ** 2)
			if (d < .075):
				ready[i] = 1
		dxu = unicycle_position_controller(x, initial_conditions)
		dxu = uni_barrier_cert(dxu, x)
		twist.linear.x = dxu[0,p]/5.
		twist.linear.y = 0.0
		twist.linear.z = 0.0
		twist.angular.x = 0
		twist.angular.y = 0
		twist.angular.z = dxu[1,p]/5.
		publisher.publish(twist)
		
	if ready[0] == 1 and ready[1] == 1 and ready[2] == 1 and ready[3] == 1:

		dxu = np.array([[0,0,0,0],[0,0,0,0]])

		print("x is",x)
		x_si = uni_to_si_states(x)

		# robot i
		xx = np.reshape(x[:, p], (3, 1))
		xi = np.reshape(x_si[:, p], (2, 1))
		mask = np.arange(x_si.shape[1]) != p
		xo = x_si[:, mask]  # for obstacles
		xgoal = goal_points[0:2, p].reshape((2,-1))
		dx = si_barrier_cert(xi*10, xo*10, xgoal*10)
		dx = np.array([[dx[0]],[dx[1]]])

		du = si_to_uni_dyn(dx, xx)

		dxu[0, p] = du[0, 0]
		dxu[1, p] = du[1, 0]


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
