import sys, os, argparse, cv2, glob, math, random, json
FileDirPath = os.path.dirname(os.path.realpath(__file__))
from tk3dv import pyEasel
from PyQt5.QtWidgets import QApplication
import PyQt5.QtCore as QtCore
from PyQt5.QtGui import QKeyEvent, QMouseEvent, QWheelEvent

from EaselModule import EaselModule
from Easel import Easel
import numpy as np
import OpenGL.GL as gl
from tk3dv.nocstools import datastructures as ds
from tk3dv.nocstools import obj_loader

from palettable.tableau import Tableau_20, BlueRed_12, ColorBlind_10, GreenOrange_12
from palettable.cartocolors.diverging import Earth_2
import calibration
from tk3dv.common import drawing, utilities
from tk3dv.extern import quaternions

class NOCSMapModule(EaselModule):
    def __init__(self):
        super().__init__()

    def init(self, InputArgs=None):
        self.Parser = argparse.ArgumentParser(description='NOCSMapModule to visualize NOCS maps and camera poses.', fromfile_prefix_chars='@')
        ArgGroup = self.Parser.add_argument_group()
        ArgGroup.add_argument('--nocs-maps', nargs='+', help='Specify input NOCS maps. * globbing is supported.', required=True)
        ArgGroup.add_argument('--colors', nargs='+', help='Specify RGB images corresponding to the input NOCS maps. * globbing is supported.', required=False)
        ArgGroup.add_argument('--intrinsics', help='Specify the intrinsics file to estimate camera pose.', required=False, default=None)
        ArgGroup.add_argument('--poses', nargs='+',
                              help='Specify the camera extrinsics corresponding to the input NOCS maps. * globbing is supported.',
                              required=False)
        ArgGroup.add_argument('--models', nargs='+',
                              help='Specify OBJ models to load additionally. * globbing is supported.',
                              required=False)
        ArgGroup.add_argument('--num-points', help='Specify the number of pixels to use for camera pose registration.', default=1000, type=int, required=False)
        ArgGroup.add_argument('--error-viz', help='Specify error wrto Nth NOCS map. If multiple NOCS maps are provided. Will compute the L2 errors between the Nth NOCS map and the rest. Will render this instead of RGB or colors.', default=-1, type=int, required=False)

        ArgGroup.add_argument('--est-pose', help='Choose to estimate pose.', action='store_true')
        self.Parser.set_defaults(est_pose=False)
        ArgGroup.add_argument('--pose-scale', help='Specify the (inverse) scale of the camera positions in the ground truth pose files.', default=1.0, type=float, required=False)

        self.Args, _ = self.Parser.parse_known_args(InputArgs)
        if len(sys.argv) <= 1:
            self.Parser.print_help()
            exit()

        self.NOCSMaps = []
        self.NOCS = []
        self.Cameras = []
        self.CamRots = []
        self.CamPos = []
        self.CamIntrinsics = []
        self.CamFlip = [] # This is a hack
        # Extrinsics, if provided
        self.PosesRots = []
        self.PosesPos = []
        self.OBJModels = []
        self.PointSize = 3
        self.Intrinsics = None
        if self.Args.intrinsics is not None:
            self.Intrinsics = ds.CameraIntrinsics()
            self.Intrinsics.init_with_file(self.Args.intrinsics)
            self.ImageSize = (self.Intrinsics.Width, self.Intrinsics.Height)
            print('[ INFO ]: Intrinsics provided. Will re-size all images to', self.ImageSize)
        if self.Intrinsics is None:
            self.ImageSize = None
            print('[ INFO ]: No intrinsics provided. Will use NOCS map size.')

        sys.stdout.flush()
        self.nNM = 0
        self.SSCtr = 0
        self.takeSS = False
        self.showNOCS = True
        self.showBB = False
        self.showPoints = False
        self.showWireFrame = False
        self.isVizError = False
        self.showOBJModels = True
        self.loadData()
        self.generateDiffMap()

    def drawNOCS(self, lineWidth=2.0, ScaleX=1, ScaleY=1, ScaleZ=1, OffsetX=0, OffsetY=0, OffsetZ=0):
        gl.glPushMatrix()

        gl.glScale(ScaleX, ScaleY, ScaleZ)
        gl.glTranslate(OffsetX, OffsetY, OffsetZ)  # Center on cube center
        drawing.drawUnitWireCube(lineWidth, True)

        gl.glPopMatrix()

    def generateDiffMap(self):
        if self.Args.error_viz != -1:
            self.isVizError = True
            self.ErrorReferenceNM = self.Args.error_viz
            if self.ErrorReferenceNM >= len(self.NOCSMaps):
                print('[ WARN ]: Resetting reference NOCS map to 0 for error computation.')
                self.ErrorReferenceNM = 0 # Reset to first


        if self.isVizError == True:
            for i in range(0, len(self.NOCSMaps)):
                DM = self.NOCSMaps[i].astype(np.float)-self.NOCSMaps[self.ErrorReferenceNM].astype(np.float) # Convert to float
                Norm = np.linalg.norm(DM, axis=2)
                Frac = 10
                NormFact = (441.6729 / Frac) / 255 # Maximum possible error in NOCS is 441.6729 == sqrt(3 * 255^2). Let's take a fraction of that
                Norm = Norm / (NormFact)
                Norm = Norm.astype(np.uint8)
                NormCol = cv2.applyColorMap(Norm, cv2.COLORMAP_JET)
                #cv2.imwrite('norm_{}.png'.format(str(i).zfill(3)), NormCol)
                self.NOCS[i] = ds.NOCSMap(self.NOCSMaps[i], RGB=cv2.cvtColor(NormCol, cv2.COLOR_BGR2RGB))# IMPORTANT: OpenCV loads as BGR, so convert to RGB

    @staticmethod
    def estimateCameraPoseFromNM(NOCSMap, NOCS, N=None, Intrinsics=None):
        ValidIdx = np.where(np.all(NOCSMap != [255, 255, 255], axis=-1)) # row, col

        # Create correspondences tuple list
        x = np.array([ValidIdx[1], ValidIdx[0]]) # row, col ==> u, v
        # Convert image coordinates from top left to bottom right (See Figure 6.2 in HZ)
        x[0, :] = NOCSMap.shape[1] - x[0, :]
        x[1, :] = NOCSMap.shape[0] - x[1, :]

        X = NOCS.Points.T

        # Subsample
        # Enough to do pose estimation from a subset of points but randomly distributed in the image
        MaxN = x.shape[1]
        if N is not None:
            MaxN = min(N, x.shape[1])
        RandIdx = [i for i in range(0, x.shape[1])]
        random.shuffle(RandIdx)
        print('[ INFO ]: Using {} points for estimating camera pose'.format(MaxN))
        sys.stdout.flush()
        RandIdx = RandIdx[:MaxN]
        x = x[:, RandIdx]
        X = X[:, RandIdx]

        X = X.astype(np.float32)
        x = x.astype(np.float32)

        if Intrinsics is not None:
            # ---------------------------------
            # If you are using Python:
            # Numpy array slices won't work as input because solvePnP requires contiguous arrays (enforced by the assertion using cv::Mat::checkVector() around line 55 of modules/calib3d/src/solvepnp.cpp version 2.4.9)
            # The P3P algorithm requires image points to be in an array of shape (N,1,2) due to its calling of cv::undistortPoints (around line 75 of modules/calib3d/src/solvepnp.cpp version 2.4.9) which requires 2-channel information.
            # Thus, given some data D = np.array(...) where D.shape = (N,M), in order to use a subset of it as, e.g., imagePoints, one must effectively copy it into a new array: imagePoints = np.ascontiguousarray(D[:,:2]).reshape((N,1,2))
            # ---------------------------------

            x = np.copy(x.T).reshape((MaxN, 1, 2))
            X = np.copy(X.T).reshape((MaxN, 1, 3))

            RetVal, rvec, tvec, Inliers = cv2.solvePnPRansac(X, x, Intrinsics.Matrix, Intrinsics.DistCoeffs, iterationsCount=10000, reprojectionError=0.001, confidence=0.9999999, flags=cv2.SOLVEPNP_ITERATIVE)
            # print(Inliers)
            # RetVal, rvec, tvec = cv2.solvePnP(X, x, Intrinsics.Matrix, Intrinsics.DistCoeffs, flags=cv2.SOLVEPNP_EPNP)

            # print(x.shape)
            # print(X.shape)
            # print(RetVal)

            k = Intrinsics.Matrix
            r, _ = cv2.Rodrigues(rvec) # Also outputs Jacobian
            c = -r.T @ tvec

            print('K-based estimate:\n')
            print('R:\n', r, '\n')
            print('C:\n', c, '\n')
            print('K:\n', k, '\n\n')

            return None, k, r, c, False

        else:
            Corr = []
            for i in range(0, max(X.shape)):
                Corr.append((x[:, i], X[:, i]))

            p, c, k, r, Flip = calibration.calculateCameraParameters(Corr)

            # # Rotation about z-axis by 180
            # r = utilities.rotation_matrix(np.array([0, 0, 1]), math.pi) @ r # TODO: This is incorrect

            print('Full estimate:\n')
            print('R:\n', r, '\n')
            print('C:\n', c, '\n')
            print('K:\n', k, '\n\n')

            return p, k, r, c, Flip

    @staticmethod
    def getFileNames(InputList):
        if InputList is None:
            return []
        FileNames = []
        for File in InputList:
            if '*' in File:
                GlobFiles = glob.glob(File, recursive=False)
                GlobFiles.sort()
                FileNames.extend(GlobFiles)
            else:
                FileNames.append(File)

        return FileNames

    def resizeAndPad(self, Image):
        # SquareUpSize = min(self.ImageSize[0], self.ImageSize[1])
        # TODO
        print('[ INFO ]: Original input size ', Image.shape)
        Image = cv2.resize(Image, self.ImageSize, interpolation=cv2.INTER_NEAREST)
        print('[ INFO ]: Input resized to ', Image.shape)
        sys.stdout.flush()

        return Image

    def loadData(self):
        Palette = ColorBlind_10
        NMFiles = self.getFileNames(self.Args.nocs_maps)
        ColorFiles = [None] * len(NMFiles)
        PoseFiles = [None] * len(NMFiles)
        if self.Args.colors is not None:
            ColorFiles = self.getFileNames(self.Args.colors)
        if self.Args.poses is not None:
            PoseFiles = self.getFileNames(self.Args.poses)

        for (NMF, CF, PF) in zip(NMFiles, ColorFiles, PoseFiles):
            NOCSMap = cv2.imread(NMF, -1)
            NOCSMap = NOCSMap[:, :, :3] # Ignore alpha if present
            NOCSMap = cv2.cvtColor(NOCSMap, cv2.COLOR_BGR2RGB) # IMPORTANT: OpenCV loads as BGR, so convert to RGB
            if self.Intrinsics is None:
                self.ImageSize = (NOCSMap.shape[1], NOCSMap.shape[0])
            else:
                NOCSMap = self.resizeAndPad(NOCSMap)
            CFIm = None
            if CF is not None:
                CFIm = cv2.imread(CF)
                CFIm = cv2.cvtColor(CFIm, cv2.COLOR_BGR2RGB) # IMPORTANT: OpenCV loads as BGR, so convert to RGB
                if CFIm.shape != NOCSMap.shape: # Re-size only if not the same size as NOCSMap
                    CFIm = cv2.resize(CFIm, (NOCSMap.shape[1], NOCSMap.shape[0]), interpolation=cv2.INTER_CUBIC) # Ok to use cubic interpolation for RGB
            NOCS = ds.NOCSMap(NOCSMap, RGB=CFIm)
            self.NOCSMaps.append(NOCSMap)
            self.NOCS.append(NOCS)

            if self.Args.est_pose == True:
                _, K, R, C, Flip = self.estimateCameraPoseFromNM(NOCSMap, NOCS, N=self.Args.num_points, Intrinsics=self.Intrinsics) # The rotation and translation are about the NOCS origin
                self.CamIntrinsics.append(K)
                self.CamRots.append(R)
                self.CamPos.append(C)
                self.CamFlip.append(Flip)
                self.Cameras.append(ds.Camera(ds.CameraExtrinsics(self.CamRots[-1], self.CamPos[-1]), ds.CameraIntrinsics(self.CamIntrinsics[-1])))

            if PF is not None:
                with open(PF) as f:
                    data = json.load(f)
                    # Loading convention: Flip sign of x postiion, flip signs of quaternion z, w
                    P = np.array([data['position']['x'], data['position']['y'], data['position']['z']]) / self.Args.pose_scale
                    Quat = np.array([data['rotation']['w'], data['rotation']['x'], data['rotation']['y'], data['rotation']['z']]) # NOTE: order is w, x, y, z
                    # Cajole transforms to work
                    P[0] *= -1
                    P += 0.5
                    Quat = np.array([Quat[0], Quat[1], -Quat[2], -Quat[3]])

                    self.PosesPos.append(P)
                    R = quaternions.quat2mat(Quat).T
                    self.PosesRots.append(R)
            else:
                self.PosesPos.append(None)
                self.PosesRots.append(None)

        self.nNM = len(NMFiles)
        self.activeNMIdx = self.nNM # len(NMFiles) will show all

        # Load OBJ models
        ModelFiles = self.getFileNames(self.Args.models)
        for MF in ModelFiles:
            self.OBJModels.append(obj_loader.Loader(MF, isNormalize=True))

    def step(self):
        pass

    def drawCamera(self, R, C, isF = False, Color=None):
        gl.glPushMatrix()

        ScaleRotMat = np.identity(4)
        ScaleRotMat[:3, :3] = R

        gl.glTranslate(C[0], C[1], C[2])
        gl.glMultMatrixf(ScaleRotMat)
        if isF:
            gl.glRotate(180, 1, 0, 0)

        Length = 5
        CubeSide = 0.1
        
        gl.glPushMatrix()
        gl.glScale(CubeSide, CubeSide, CubeSide/2)
        gl.glTranslate(-0.5, -0.5, -0.5)
        drawing.drawUnitWireCube(1.0, WireColor=(0, 0, 0))
        gl.glPopMatrix()
        
        gl.glPushAttrib(gl.GL_LINE_BIT)
        gl.glLineWidth(1.0)
        gl.glPushAttrib(gl.GL_ENABLE_BIT)
        gl.glLineStipple(1, 0xAAAA)  # [1]
        gl.glEnable(gl.GL_LINE_STIPPLE)      
        
        gl.glBegin(gl.GL_LINES)
        gl.glColor3f(1.0, 0.0, 0.0)
        gl.glVertex3f(0.0, 0.0, 0.0)
        gl.glVertex3f(0.0, 0.0, Length) # Always in the negative z

        gl.glEnd()

        gl.glPopAttrib()
        gl.glPopAttrib()
    
        # Offset = 5
        #drawing.drawAxes(Offset + 0.2, Color=Color)
        gl.glPopMatrix()

    def draw(self):
        gl.glMatrixMode(gl.GL_MODELVIEW)
        gl.glPushMatrix()

        ScaleFact = 500
        gl.glTranslate(-ScaleFact/2, -ScaleFact/2, -ScaleFact/2)
        gl.glScale(ScaleFact, ScaleFact, ScaleFact)
        for Idx, NOCS in enumerate(self.NOCS):
            if self.activeNMIdx != self.nNM:
                if Idx != self.activeNMIdx:
                    continue

            if self.showPoints:
                NOCS.draw(self.PointSize)
            else:
                NOCS.drawConn(isWireFrame=self.showWireFrame)
            if self.showBB:
                NOCS.drawBB()

        CamAxisLength = 0.1
        for Idx, (K, R, C, isF) in enumerate(zip(self.CamIntrinsics, self.CamRots, self.CamPos, self.CamFlip), 0):
            if self.activeNMIdx != self.nNM:
                if Idx != self.activeNMIdx:
                    continue

            self.Cameras[Idx].draw(isDrawDir=True, isFlip=isF, Color=np.array([1.0, 0.0, 0.0]), Length=CamAxisLength, LineWidth=2.0)
        for Idx, (R_in, C_in) in enumerate(zip(self.PosesRots, self.PosesPos), 0):
            if self.activeNMIdx != self.nNM:
                if Idx != self.activeNMIdx:
                    continue

            if R_in is not None and C_in is not None:
                ds.Camera.drawCamera(R_in, C_in, isDrawDir=True, isFlip=False, Color=np.array([0.0, 1.0, 0.0]), Length=CamAxisLength, LineWidth=2.0)

        if self.showNOCS:
            self.drawNOCS(lineWidth=5.0)

        if self.showOBJModels:
            if self.OBJModels is not None:
                for OM in self.OBJModels:
                    OM.draw(isWireFrame=self.showWireFrame)

        gl.glPopMatrix()

        if self.takeSS:
            x, y, width, height = gl.glGetIntegerv(gl.GL_VIEWPORT)
            # print("Screenshot viewport:", x, y, width, height)
            gl.glPixelStorei(gl.GL_PACK_ALIGNMENT, 1)

            data = gl.glReadPixels(x, y, width, height, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE)
            SS = np.frombuffer(data, dtype=np.uint8)
            SS = np.reshape(SS, (height, width, 4))
            SS = cv2.flip(SS, 0)
            SS = cv2.cvtColor(SS, cv2.COLOR_BGRA2RGBA)
            cv2.imwrite('screenshot_' + str(self.SSCtr).zfill(6) + '.png', SS)
            self.SSCtr = self.SSCtr + 1
            self.takeSS = False

            # Also serialize points
            for Idx, NOCS in enumerate(self.NOCS):
                if self.activeNMIdx != self.nNM:
                    if Idx != self.activeNMIdx:
                        continue
                NOCS.serialize('nocs_' + str(Idx).zfill(2) + '_' + str(self.SSCtr).zfill(6) + '.obj')
            print('[ INFO ]: Done saving.')
            sys.stdout.flush()


    def keyPressEvent(self, a0: QKeyEvent):
        if a0.key() == QtCore.Qt.Key_Plus:  # Increase or decrease point size
            if self.PointSize < 20:
                self.PointSize = self.PointSize + 1

        if a0.modifiers() != QtCore.Qt.NoModifier:
            return

        if a0.key() == QtCore.Qt.Key_Minus:  # Increase or decrease point size
            if self.PointSize > 1:
                self.PointSize = self.PointSize - 1

        if a0.key() == QtCore.Qt.Key_T:  # Toggle NOCS views
            if self.nNM > 0:
                self.activeNMIdx = (self.activeNMIdx + 1)%(self.nNM+1)

        if a0.key() == QtCore.Qt.Key_N:
            self.showNOCS = not self.showNOCS
        if a0.key() == QtCore.Qt.Key_B:
            self.showBB = not self.showBB
        if a0.key() == QtCore.Qt.Key_M:
            self.showOBJModels = not self.showOBJModels
        if a0.key() == QtCore.Qt.Key_P:
            self.showPoints = not self.showPoints
        if a0.key() == QtCore.Qt.Key_W:
            self.showWireFrame = not self.showWireFrame
        if a0.key() == QtCore.Qt.Key_S:
            print('[ INFO ]: Taking snapshot and saving active NOCS maps as OBJ. This might take a while...')
            sys.stdout.flush()
            self.takeSS = True


if __name__ == '__main__':
    app = QApplication(sys.argv)

    mainWindow = Easel([NOCSMapModule()], sys.argv[1:])
    mainWindow.show()
    sys.exit(app.exec_())
